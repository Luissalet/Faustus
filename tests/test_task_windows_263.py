"""#263 -- run windows, period keys, pause and closed statuses for scheduled
tasks. Everything runs on a fake clock (`src.task_scheduler._utcnow` is
patched) over a real SQLite file; the per-task guards are stored in the real
`task_policies.db`, so every task id here is unique per run.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import src.task_scheduler as ts
from src.task_windows import (
    CLOSED_STATUSES,
    closed_status_for,
    guard_anchor,
    normalize_period_key,
    normalize_window_minutes,
    period_value,
    window_violation,
)
from tests.test_scheduler_lease_claim import add_task, db, make_scheduler, row  # noqa: F401


def tid(prefix="w263") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


class Clock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self):
        return self.now


@pytest.fixture()
def clock(monkeypatch):
    c = Clock(datetime(2026, 10, 10, 9, 0, 5))
    monkeypatch.setattr(ts, "_utcnow", c)
    return c


@pytest.fixture()
def quiet(monkeypatch):
    """No foreground activity, no breaker: only the guards decide."""
    import src.interactive_gate as gate
    state = {"foreground": False}
    monkeypatch.setattr(gate, "has_foreground_activity", lambda: state["foreground"])
    return state


def runs_of(dbm, task_id):
    s = dbm.SessionLocal()
    try:
        return (s.query(dbm.TaskRun).filter(dbm.TaskRun.task_id == task_id)
                .order_by(dbm.TaskRun.started_at.asc()).all())
    finally:
        s.close()


def set_next_run(dbm, task_id, when):
    s = dbm.SessionLocal()
    try:
        s.query(dbm.ScheduledTask).filter(dbm.ScheduledTask.id == task_id).update({"next_run": when})
        s.commit()
    finally:
        s.close()


def set_status(dbm, task_id, status):
    s = dbm.SessionLocal()
    try:
        s.query(dbm.ScheduledTask).filter(dbm.ScheduledTask.id == task_id).update({"status": status})
        s.commit()
    finally:
        s.close()


async def tick(sch, dispatched):
    """One scheduler poll; `dispatched` collects the ids that would run."""
    async def fake_execute(task_id, **kwargs):
        dispatched.append(task_id)

    sch._execute_task = fake_execute
    await sch._check_due_tasks()
    await asyncio.sleep(0.02)


# ── pure logic ────────────────────────────────────────────────────────────

def test_window_and_period_values_validate():
    assert normalize_window_minutes(None) is None
    assert normalize_window_minutes("") is None
    assert normalize_window_minutes(0) is None
    assert normalize_window_minutes("45") == 45
    for bad in (-5, "abc", 10 ** 7):
        with pytest.raises(ValueError):
            normalize_window_minutes(bad)
    assert normalize_period_key(None) is None
    assert normalize_period_key("") is None
    assert normalize_period_key(" ISO_WEEK ") == "iso_week"
    with pytest.raises(ValueError):
        normalize_period_key("fortnight")


def test_period_value_follows_the_task_time_zone():
    # 2026-12-31 23:30 UTC is already 2027-01-01 in Madrid -- and ISO week 53 of 2026.
    t = datetime(2026, 12, 31, 23, 30)
    assert period_value(t, None, "day") == "2026-12-31"
    assert period_value(t, "Europe/Madrid", "day") == "2027-01-01"
    assert period_value(t, "Europe/Madrid", "iso_week") == "2026-W53"
    assert period_value(t, "Europe/Madrid", "month") == "2027-01"
    assert period_value(t, "not/a_zone", "month") == "2026-12"   # falls back to UTC
    with pytest.raises(ValueError):
        period_value(t, None, "year")


def test_window_boundary_is_inclusive_and_names_the_numbers():
    due = datetime(2026, 10, 10, 9, 0, 0)
    assert window_violation(due, due + timedelta(minutes=30), 30) is None
    assert window_violation(due, due + timedelta(minutes=5), None) is None
    reason = window_violation(due, due + timedelta(minutes=30, seconds=1), 30)
    assert reason and "30 min" in reason and "2026-10-10 09:00:00" in reason


def test_guard_anchor_prefers_the_first_due_instant():
    due, origin = datetime(2026, 10, 10, 9, 30), datetime(2026, 10, 10, 9, 0)
    assert guard_anchor(origin, due) == origin
    assert guard_anchor(None, due) == due
    assert guard_anchor(datetime(2026, 10, 10, 10, 0), due) == due   # an origin after the due time is nonsense


@pytest.mark.parametrize("status,result,error,closed", [
    ("success", "all good", None, "ok"),
    ("success", "done", "one step failed", "partial"),
    ("partial", "half", None, "partial"),
    ("error", None, "ConnectionError: boom", "failed"),
    ("error", None, "IMAP authentication failed for luis@x", "blocked-login"),
    ("error", None, "HTTP 401 Unauthorized", "blocked-login"),
    ("aborted", None, "Server restarted while task was running", "failed"),
    ("skipped", "nothing new", None, "ok"),
    ("skipped", None, "Task is paused", "skipped-paused"),
    ("skipped-window", "late", None, "skipped-window"),
    ("skipped-period", "dup", None, "skipped-period"),
    ("skipped-paused", "off", None, "skipped-paused"),
    ("fenced", None, "another worker took over", "skipped-period"),
])
def test_closed_status_mapping(status, result, error, closed):
    got, reason = closed_status_for(status, result, error)
    assert got == closed
    assert got in CLOSED_STATUSES
    assert "\n" not in reason and len(reason) <= 160


def test_open_runs_have_no_closed_status():
    assert closed_status_for("running", "Starting…")[0] is None
    assert closed_status_for("queued", "Queued")[0] is None


def test_guard_storage_roundtrip_and_validation():
    t = tid()
    assert ts.get_task_schedule_guard(t)["window_minutes"] is None
    ts.set_task_schedule_guard(t, window_minutes=45, period_key="iso_week")
    g = ts.get_task_schedule_guard(t)
    assert (g["window_minutes"], g["period_key"]) == (45, "iso_week")
    ts.set_task_schedule_guard(t, period_key=None)            # clears only the period
    g = ts.get_task_schedule_guard(t)
    assert (g["window_minutes"], g["period_key"]) == (45, None)
    ts.set_task_schedule_guard(t, window_minutes=0)           # 0 clears the window
    assert ts.get_task_schedule_guard(t)["window_minutes"] is None
    with pytest.raises(ValueError):
        ts.set_task_schedule_guard(t, period_key="decade")
    assert t not in ts.guarded_task_ids()
    ts.set_task_schedule_guard(t, period_key="day")
    assert t in ts.guarded_task_ids()
    # the policy dict every other caller compares for equality is untouched
    assert set(ts.get_task_policy(t)) == {"dst_ambiguity_policy", "misfire_policy", "budget_preset",
                                          "permissions", "connector_ids"}


# ── the scheduler, on a fake clock ────────────────────────────────────────

async def test_a_task_without_guards_behaves_exactly_as_before(db, clock, quiet):
    t = tid()
    clock.now = datetime(2026, 10, 10, 13, 12, 0)             # four hours after its 09:00 slot
    add_task(db, t, due=datetime(2026, 10, 10, 9, 0, 0))
    sch, dispatched = make_scheduler(), []
    await tick(sch, dispatched)
    assert dispatched == [t]
    assert runs_of(db, t) == []                               # nothing recorded by the guards


async def test_catch_up_inside_the_window_runs(db, clock, quiet):
    t = tid()
    add_task(db, t, due=datetime(2026, 10, 10, 9, 0, 0))
    ts.set_task_schedule_guard(t, window_minutes=30)
    clock.now = datetime(2026, 10, 10, 9, 20, 0)              # 20 min late, window 30
    sch, dispatched = make_scheduler(), []
    await tick(sch, dispatched)
    assert dispatched == [t]
    assert runs_of(db, t) == []


async def test_catch_up_outside_the_window_is_skipped_with_a_reason(db, clock, quiet):
    t = tid()
    add_task(db, t, due=datetime(2026, 10, 10, 9, 0, 0))
    ts.set_task_schedule_guard(t, window_minutes=30)
    clock.now = datetime(2026, 10, 10, 13, 12, 0)             # 252 min late
    sch, dispatched = make_scheduler(), []
    await tick(sch, dispatched)
    assert dispatched == []
    rows = runs_of(db, t)
    assert len(rows) == 1
    assert rows[0].status == "skipped-window"
    assert "252 min late" in rows[0].result and "30 min" in rows[0].result
    r = row(db, t)
    assert r.next_run == datetime(2026, 10, 11, 9, 0, 0)      # the schedule moved on
    assert r.last_run is None and (r.run_count or 0) == 0     # and nothing counts as having run
    assert r.lease_owner is None                              # the lease was handed back
    # a second tick finds nothing due: the skip is recorded once
    await tick(sch, dispatched)
    assert len(runs_of(db, t)) == 1 and dispatched == []
    assert closed_status_for(rows[0].status, rows[0].result, rows[0].error)[0] == "skipped-window"


async def test_postponing_cannot_push_a_task_past_its_window(db, clock, quiet):
    """The foreground keeps the task waiting 15 min at a time: the window is
    measured from when it was first due, not from the last postponement."""
    t = tid()
    add_task(db, t, due=datetime(2026, 10, 10, 9, 0, 0))
    ts.set_task_schedule_guard(t, window_minutes=30)
    sch, dispatched = make_scheduler(), []
    quiet["foreground"] = True
    clock.now = datetime(2026, 10, 10, 9, 0, 5)
    await tick(sch, dispatched)                               # deferred to 09:15:05
    clock.now = datetime(2026, 10, 10, 9, 15, 10)
    await tick(sch, dispatched)                               # deferred to 09:30:10
    assert dispatched == [] and runs_of(db, t) == []
    quiet["foreground"] = False
    clock.now = datetime(2026, 10, 10, 9, 31, 0)              # 31 min after the ORIGINAL slot
    await tick(sch, dispatched)
    assert dispatched == []
    assert [r.status for r in runs_of(db, t)] == ["skipped-window"]


async def test_a_postponement_inside_the_window_still_runs(db, clock, quiet):
    t = tid()
    add_task(db, t, due=datetime(2026, 10, 10, 9, 0, 0))
    ts.set_task_schedule_guard(t, window_minutes=30)
    sch, dispatched = make_scheduler(), []
    quiet["foreground"] = True
    clock.now = datetime(2026, 10, 10, 9, 0, 5)
    await tick(sch, dispatched)
    quiet["foreground"] = False
    clock.now = datetime(2026, 10, 10, 9, 15, 10)
    await tick(sch, dispatched)
    assert dispatched == [t] and runs_of(db, t) == []


async def test_duplicate_firing_in_the_same_period_is_skipped(db, clock, quiet):
    t = tid()
    add_task(db, t, due=datetime(2026, 10, 10, 9, 0, 0))
    ts.set_task_schedule_guard(t, period_key="day")
    sch, dispatched = make_scheduler(), []
    await tick(sch, dispatched)
    assert dispatched == [t]
    sch._executing.discard(t)
    sch._release_lease(t)
    sch._close_period(t, "success", "done", None, "run-1")    # what the run's end does

    # the same slot fires again (a restart, a second worker, a manual re-queue)
    set_next_run(db, t, datetime(2026, 10, 10, 9, 0, 30))
    clock.now = datetime(2026, 10, 10, 9, 1, 0)
    await tick(sch, dispatched)
    assert dispatched == [t]                                  # still the one dispatch
    rows = runs_of(db, t)
    assert [r.status for r in rows] == ["skipped-period"]
    assert "2026-10-10" in rows[0].result

    # the next day is a new period
    set_next_run(db, t, datetime(2026, 10, 11, 9, 0, 0))
    clock.now = datetime(2026, 10, 11, 9, 0, 5)
    await tick(sch, dispatched)
    assert dispatched == [t, t]


async def test_iso_week_and_month_periods(db, clock, quiet):
    t_week, t_month = tid(), tid()
    for t, key in ((t_week, "iso_week"), (t_month, "month")):
        add_task(db, t, due=datetime(2026, 10, 5, 9, 0, 0))   # Monday of ISO week 41
        ts.set_task_schedule_guard(t, period_key=key)
    clock.now = datetime(2026, 10, 5, 9, 0, 5)
    sch, dispatched = make_scheduler(), []
    await tick(sch, dispatched)
    assert sorted(dispatched) == sorted([t_week, t_month])
    for t in (t_week, t_month):
        sch._executing.discard(t)
        sch._release_lease(t)
        sch._close_period(t, "success", "done", None, "r")
    # Wednesday of the same week, same month
    for t in (t_week, t_month):
        set_next_run(db, t, datetime(2026, 10, 7, 9, 0, 0))
    clock.now = datetime(2026, 10, 7, 9, 0, 5)
    await tick(sch, dispatched)
    assert len(dispatched) == 2
    assert [r.status for r in runs_of(db, t_week)] == ["skipped-period"]
    assert [r.status for r in runs_of(db, t_month)] == ["skipped-period"]
    # Monday of the next week: the week is open again, the month is not
    for t in (t_week, t_month):
        set_next_run(db, t, datetime(2026, 10, 12, 9, 0, 0))
    clock.now = datetime(2026, 10, 12, 9, 0, 5)
    await tick(sch, dispatched)
    assert dispatched.count(t_week) == 2 and dispatched.count(t_month) == 1
    assert [r.status for r in runs_of(db, t_month)] == ["skipped-period", "skipped-period"]


async def test_a_run_blocked_at_the_login_leaves_the_period_open(db, clock, quiet):
    t = tid()
    add_task(db, t, due=datetime(2026, 10, 10, 9, 0, 0))
    ts.set_task_schedule_guard(t, period_key="day")
    sch, dispatched = make_scheduler(), []
    await tick(sch, dispatched)
    sch._executing.discard(t)
    sch._release_lease(t)
    sch._close_period(t, "error", "IMAP authentication failed", None, "run-1")
    set_next_run(db, t, datetime(2026, 10, 10, 10, 0, 0))     # the retry slot, same day
    clock.now = datetime(2026, 10, 10, 10, 0, 5)
    await tick(sch, dispatched)
    assert dispatched == [t, t] and runs_of(db, t) == []
    # a plain failure, on the other hand, does use the period up
    sch._executing.discard(t)
    sch._release_lease(t)
    sch._close_period(t, "error", "ValueError: nope", None, "run-2")
    set_next_run(db, t, datetime(2026, 10, 10, 11, 0, 0))
    clock.now = datetime(2026, 10, 10, 11, 0, 5)
    await tick(sch, dispatched)
    assert [r.status for r in runs_of(db, t)] == ["skipped-period"]


async def test_a_late_run_is_filed_under_the_period_it_was_due_in(db, clock, quiet):
    """Due 23:50 on the 9th, the PC wakes at 00:20 on the 10th: that run belongs
    to the 9th, so the 10th's own 23:50 slot is not blocked by it."""
    t = tid()
    add_task(db, t, due=datetime(2026, 10, 9, 23, 50, 0), scheduled_time="23:50")
    ts.set_task_schedule_guard(t, period_key="day")
    clock.now = datetime(2026, 10, 10, 0, 20, 0)
    sch = make_scheduler()
    sch._known_task_owners = lambda: []
    await sch.start()                                         # the startup sweep pushes it to now+60 s
    try:
        pushed = row(db, t).next_run
        assert pushed == clock.now + timedelta(seconds=60)
        clock.now = pushed + timedelta(seconds=1)
        dispatched = []
        await tick(sch, dispatched)
        assert dispatched == [t]
        anchor, pv = sch._guard_anchor_map()[t]
        assert anchor == datetime(2026, 10, 9, 23, 50, 0) and pv == "2026-10-09"
    finally:
        await sch.stop()


async def test_startup_sweep_skips_a_task_whose_window_closed_while_off(db, clock, quiet):
    late, inside, plain = tid(), tid(), tid()
    for t in (late, inside, plain):
        add_task(db, t, due=datetime(2026, 10, 10, 9, 0, 0))
    ts.set_task_schedule_guard(late, window_minutes=30)
    ts.set_task_schedule_guard(inside, window_minutes=30)
    clock.now = datetime(2026, 10, 10, 9, 10, 0)
    set_next_run(db, late, datetime(2026, 10, 10, 8, 0, 0))   # off since 08:00: 70 min late at 09:10
    sch = make_scheduler()
    sch._known_task_owners = lambda: []
    await sch.start()
    try:
        r_late = row(db, late)
        assert [x.status for x in runs_of(db, late)] == ["skipped-window"]
        assert r_late.next_run == datetime(2026, 10, 11, 9, 0, 0)
        # 10 min late and a 30 min window: pushed to run, nothing recorded
        assert runs_of(db, inside) == []
        assert row(db, inside).next_run == clock.now + timedelta(seconds=60)
        # no guard: the historical behaviour, untouched
        assert runs_of(db, plain) == []
        assert row(db, plain).next_run == clock.now + timedelta(seconds=60)
    finally:
        await sch.stop()


async def test_a_manual_run_of_a_paused_task_is_recorded_skipped_paused(db, clock):
    t = tid()
    add_task(db, t, due=datetime(2026, 10, 11, 9, 0, 0))
    set_status(db, t, "paused")
    s = db.SessionLocal()
    try:
        s.add(db.TaskRun(id="r-" + t, task_id=t, started_at=clock.now, status="queued",
                         result="Queued — waiting for a free slot…"))
        s.commit()
    finally:
        s.close()
    sch = make_scheduler()
    await sch._execute_task_locked(t, "r-" + t, gate_foreground=False)
    rows = runs_of(db, t)
    assert [r.status for r in rows] == ["skipped-paused"]
    assert "paused" in rows[0].result.lower() and rows[0].error is None
    assert closed_status_for(rows[0].status, rows[0].result, rows[0].error)[0] == "skipped-paused"


async def test_a_paused_task_that_declared_a_guard_logs_the_slots_it_missed(db, clock, quiet):
    guarded, plain = tid(), tid()
    for t in (guarded, plain):
        add_task(db, t, due=datetime(2026, 10, 10, 9, 0, 0))
        set_status(db, t, "paused")
    ts.set_task_schedule_guard(guarded, period_key="day")
    clock.now = datetime(2026, 10, 10, 9, 0, 30)
    sch, dispatched = make_scheduler(), []
    await tick(sch, dispatched)
    await tick(sch, dispatched)                               # twice: still one row
    assert dispatched == []
    assert [r.status for r in runs_of(db, guarded)] == ["skipped-paused"]
    assert row(db, guarded).next_run == datetime(2026, 10, 11, 9, 0, 0)
    assert row(db, guarded).status == "paused"                # pausing is not undone
    assert runs_of(db, plain) == []                           # no guard: writes nothing new


async def test_a_real_run_ends_with_a_closed_status_and_uses_up_its_period(db, clock, quiet, monkeypatch):
    """Through `_execute_task_locked` itself: an action task whose action
    succeeds, then a duplicate firing of the same day."""
    t = tid()
    s = db.SessionLocal()
    try:
        s.add(db.ScheduledTask(
            id=t, owner="luis", name=t, task_type="action", action="noop_for_test",
            schedule="daily", scheduled_time="09:00", trigger_type="schedule",
            next_run=datetime(2026, 10, 10, 9, 0, 0), status="active",
            output_target="none", notifications_enabled=False))
        s.commit()
    finally:
        s.close()
    ts.set_task_schedule_guard(t, period_key="day", window_minutes=60)
    sch = make_scheduler()

    async def fake_action(task, run_id=None):
        return "did the thing", True

    sch._execute_action = fake_action
    monkeypatch.setattr(ts, "notify_task_finished", lambda **kw: None)
    monkeypatch.setattr(sch, "_log_to_assistant", lambda *a, **k: None)

    # a real dispatch: the gate admits it, the run executes, the period closes
    due = datetime(2026, 10, 10, 9, 0, 0)
    assert sch._claim_due_task(t, due, clock.now) is True
    assert sch._guard_gate(t, due, clock.now) is True
    s = db.SessionLocal()
    try:
        s.add(db.TaskRun(id="run-1", task_id=t, started_at=clock.now, status="queued"))
        s.commit()
    finally:
        s.close()
    await sch._execute_task_locked(t, "run-1", gate_foreground=False)
    sch._release_lease(t)
    first = runs_of(db, t)
    assert [r.status for r in first] == ["success"]
    assert closed_status_for(first[0].status, first[0].result, first[0].error)[0] == "ok"
    assert ts.period_taken(t, "2026-10-10")

    # the same day fires again
    set_next_run(db, t, datetime(2026, 10, 10, 9, 5, 0))
    clock.now = datetime(2026, 10, 10, 9, 6, 0)
    dispatched = []
    await tick(sch, dispatched)
    assert dispatched == []
    assert [r.status for r in runs_of(db, t)] == ["success", "skipped-period"]


async def test_manual_run_now_ignores_the_guards(db, clock, quiet):
    t = tid()
    add_task(db, t, due=datetime(2026, 10, 10, 9, 0, 0))
    ts.set_task_schedule_guard(t, period_key="day", window_minutes=1)
    ts.consume_period(t, "2026-10-10", "ok", "earlier")
    sch = make_scheduler()
    seen = []

    async def fake_execute(task_id, **kwargs):
        seen.append(task_id)

    sch._execute_task = fake_execute
    assert await sch.run_task_now(t) is True
    await asyncio.sleep(0.02)
    assert seen == [t]                                        # no gate on a person's request


# ── API ───────────────────────────────────────────────────────────────────

@pytest.fixture()
def client(db, monkeypatch):
    import routes.task.task_routes as task_routes
    monkeypatch.setattr(task_routes, "SessionLocal", db.SessionLocal)
    app = FastAPI()

    @app.middleware("http")
    async def auth(request: Request, call_next):
        request.state.current_user = request.headers.get("x-test-owner", "alice")
        return await call_next(request)

    scheduler = MagicMock()
    scheduler.ensure_defaults = AsyncMock()
    app.include_router(task_routes.setup_task_routes(scheduler))
    return TestClient(app)


BODY = {"name": "windowed", "prompt": "do it", "task_type": "llm", "schedule": "daily",
        "scheduled_time": "09:00", "timezone": "Europe/Madrid"}


def test_api_create_returns_and_persists_the_guards(client, db):
    r = client.post("/api/tasks", json={**BODY, "window_minutes": 45, "period_key": "iso_week"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["window_minutes"], body["period_key"], body["paused"]) == (45, "iso_week", False)
    got = client.get(f"/api/tasks/{body['id']}").json()
    assert (got["window_minutes"], got["period_key"]) == (45, "iso_week")
    listed = [x for x in client.get("/api/tasks").json()["tasks"] if x["id"] == body["id"]][0]
    assert (listed["window_minutes"], listed["period_key"]) == (45, "iso_week")


def test_api_without_guards_looks_as_before(client):
    r = client.post("/api/tasks", json=BODY)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["window_minutes"] is None and body["period_key"] is None
    assert "policy" not in body                               # no policy was declared


def test_api_update_changes_and_clears_only_what_it_names(client):
    tid_ = client.post("/api/tasks", json={**BODY, "window_minutes": 45, "period_key": "day"}).json()["id"]
    r = client.put(f"/api/tasks/{tid_}", json={"name": "renamed"})
    assert (r.json()["window_minutes"], r.json()["period_key"]) == (45, "day")      # untouched
    r = client.put(f"/api/tasks/{tid_}", json={"period_key": "month"})
    assert (r.json()["window_minutes"], r.json()["period_key"]) == (45, "month")
    r = client.put(f"/api/tasks/{tid_}", json={"window_minutes": None})
    assert (r.json()["window_minutes"], r.json()["period_key"]) == (None, "month")  # explicit null clears
    r = client.put(f"/api/tasks/{tid_}", json={"period_key": None})
    assert (r.json()["window_minutes"], r.json()["period_key"]) == (None, None)


def test_api_rejects_bad_guards(client):
    assert client.post("/api/tasks", json={**BODY, "period_key": "fortnight"}).status_code == 400
    assert client.post("/api/tasks", json={**BODY, "window_minutes": -3}).status_code == 400
    ev = {"name": "e", "prompt": "x", "trigger_type": "event", "trigger_event": "session_created",
          "trigger_count": 1, "period_key": "day"}
    assert client.post("/api/tasks", json=ev).status_code == 400
    tid_ = client.post("/api/tasks", json=BODY).json()["id"]
    assert client.put(f"/api/tasks/{tid_}", json={"window_minutes": "soon"}).status_code in (400, 422)
    assert client.put(f"/api/tasks/{tid_}", json={"period_key": "decade"}).status_code == 400


def test_api_pause_shows_and_runs_carry_closed_status_and_reason(client, db):
    tid_ = client.post("/api/tasks", json={**BODY, "window_minutes": 30}).json()["id"]
    assert client.post(f"/api/tasks/{tid_}/pause").json()["status"] == "paused"
    assert client.get(f"/api/tasks/{tid_}").json()["paused"] is True
    s = db.SessionLocal()
    try:
        now = datetime(2026, 10, 10, 13, 12, 0)
        s.add(db.TaskRun(id="a", task_id=tid_, started_at=now - timedelta(hours=2), finished_at=now,
                         status="success", result="Report ready\nmore"))
        s.add(db.TaskRun(id="b", task_id=tid_, started_at=now, finished_at=now,
                         status="skipped-window", result="Due 2026-10-10 09:00:00 UTC; reached 252 min late, the window is 30 min"))
        s.commit()
    finally:
        s.close()
    runs = client.get(f"/api/tasks/{tid_}/runs").json()["runs"]
    by_id = {r["id"]: r for r in runs}
    assert (by_id["a"]["closed_status"], by_id["a"]["reason"]) == ("ok", "Report ready")
    assert by_id["b"]["closed_status"] == "skipped-window" and "252 min late" in by_id["b"]["reason"]
    task = client.get(f"/api/tasks/{tid_}").json()
    assert task["last_run_closed_status"] == "skipped-window"
    assert "window is 30 min" in task["last_run_reason"]
    listed = [x for x in client.get("/api/tasks").json()["tasks"] if x["id"] == tid_][0]
    assert listed["last_run_closed_status"] == "skipped-window"
    recent = client.get("/api/tasks/runs/recent").json()["runs"]
    assert any(r["id"] == "b" and r["closed_status"] == "skipped-window" for r in recent)


# ── the agent tool (manage_tasks) ─────────────────────────────────────────

async def test_manage_tasks_accepts_and_reports_the_guards(db):
    from src.tools.system import do_manage_tasks
    made = await do_manage_tasks(json.dumps({
        "action": "create", "name": "tool-made", "prompt": "do it", "schedule": "weekly",
        "scheduled_time": "08:00", "scheduled_day": 0, "timezone": "Europe/Madrid",
        "window_minutes": 90, "period_key": "iso_week"}), owner="luis")
    assert made["exit_code"] == 0, made
    task_id = made["task_id"]
    g = ts.get_task_schedule_guard(task_id)
    assert (g["window_minutes"], g["period_key"]) == (90, "iso_week")

    listed = await do_manage_tasks(json.dumps({"action": "list"}), owner="luis")
    line = [l for l in listed["response"].splitlines() if task_id in l][0]
    assert "window 90 min" in line and "once per iso_week" in line

    edited = await do_manage_tasks(json.dumps({"action": "edit", "task_id": task_id,
                                               "window_minutes": 0, "period_key": "month"}), owner="luis")
    assert edited["exit_code"] == 0, edited
    g = ts.get_task_schedule_guard(task_id)
    assert (g["window_minutes"], g["period_key"]) == (None, "month")

    bad = await do_manage_tasks(json.dumps({"action": "edit", "task_id": task_id,
                                            "period_key": "decade"}), owner="luis")
    assert bad["exit_code"] == 1 and "period_key" in bad["error"]
    ev = await do_manage_tasks(json.dumps({"action": "create", "name": "x", "prompt": "p",
                                           "trigger_type": "event", "trigger_event": "session_created",
                                           "trigger_count": 1, "window_minutes": 10}), owner="luis")
    assert ev["exit_code"] == 1

    s = db.SessionLocal()
    try:
        s.add(db.TaskRun(id="h1", task_id=task_id, started_at=datetime(2026, 10, 10, 9, 0),
                         finished_at=datetime(2026, 10, 10, 9, 0), status="skipped-period",
                         result="Period 2026-10 (month) already ran"))
        s.commit()
    finally:
        s.close()
    hist = await do_manage_tasks(json.dumps({"action": "history", "task_id": task_id}), owner="luis")
    assert hist["exit_code"] == 0
    assert hist["runs"][0]["closed_status"] == "skipped-period"
    assert "already ran" in hist["runs"][0]["reason"]
    denied = await do_manage_tasks(json.dumps({"action": "history", "task_id": task_id}), owner="mallory")
    assert denied["exit_code"] == 1


def test_the_agent_tool_schema_documents_the_new_fields():
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    schema = next(t for t in FUNCTION_TOOL_SCHEMAS if t["function"]["name"] == "manage_tasks")["function"]["parameters"]
    props = schema["properties"]
    assert "history" in props["action"]["enum"]
    assert props["window_minutes"]["type"] == "integer"
    assert set(props["period_key"]["enum"]) == {"day", "iso_week", "month", ""}
