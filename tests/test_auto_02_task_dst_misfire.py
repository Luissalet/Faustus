"""QA-43 — Europe/Madrid DST transitions and machine-off misfires need an
explicit, declared policy instead of whatever zoneinfo happens to pick.

Covers:
  * `_dst_resolve`/`compute_next_run` classification and all three
    `dst_ambiguity_policy` values on the spring-forward gap (2026-03-29
    02:30, nonexistent) and the fall-back repeat (2026-10-25 02:30,
    happens twice).
  * `run_all`'s second (`fold=1`) occurrence via `TaskScheduler._next_moment_after`.
  * The startup misfire sweep (`TaskScheduler.start()`) for a machine that
    was off for three hours, under both `misfire_policy` values, against a
    real (in-memory) `ScheduledTask` row.
"""
from __future__ import annotations

import os
import tempfile
import uuid
from datetime import datetime, timedelta, timezone

import pytest

_tmp_data = tempfile.mkdtemp(prefix="odysseus-auto02-dst-test-")
os.environ.setdefault("DATA_DIR", _tmp_data)
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_tmp_data}/app.db")
os.environ["ODYSSEUS_INPROCESS_POLLERS"] = "0"

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import core.database as db_mod
from src.task_scheduler import (
    TaskScheduler, compute_next_run, _dst_resolve, _advance_past_dst,
    set_task_policy, get_task_policy,
)

MADRID = "Europe/Madrid"
GAP_DAY = datetime(2026, 3, 28, 20, 0)      # before the 2026-03-29 spring-forward
FOLD_DAY = datetime(2026, 10, 24, 20, 0)    # before the 2026-10-25 fall-back


# ---------------------------------------------------------------------------
# _dst_resolve classification
# ---------------------------------------------------------------------------

def test_dst_resolve_classifies_gap_ambiguous_and_normal():
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(MADRID)
    assert _dst_resolve(datetime(2026, 3, 29, 2, 30), tz, "run_once")[0] == "nonexistent"
    assert _dst_resolve(datetime(2026, 10, 25, 2, 30), tz, "run_once")[0] == "ambiguous"
    assert _dst_resolve(datetime(2026, 6, 1, 2, 30), tz, "run_once")[0] == "normal"


# ---------------------------------------------------------------------------
# compute_next_run — spring-forward gap (2026-03-29 02:30 never happens)
# ---------------------------------------------------------------------------

def test_compute_next_run_gap_skip_advances_to_the_next_valid_day():
    nxt = compute_next_run("daily", "02:30", after=GAP_DAY, tz_name=MADRID,
                           dst_ambiguity_policy="skip")
    # 2026-03-30 00:30 UTC == 2026-03-30 02:30 CEST — the *next* day's 02:30,
    # not the gap day.
    assert nxt == datetime(2026, 3, 30, 0, 30)


def test_compute_next_run_gap_run_once_fires_once_at_the_jump():
    nxt = compute_next_run("daily", "02:30", after=GAP_DAY, tz_name=MADRID,
                           dst_ambiguity_policy="run_once")
    assert nxt == datetime(2026, 3, 29, 1, 30)


def test_compute_next_run_gap_run_all_behaves_like_run_once_a_gap_has_only_one_instant():
    """run_all still fires exactly once for a gap — there is only one valid
    UTC instant to name, unlike an ambiguous repeat which has two."""
    nxt = compute_next_run("daily", "02:30", after=GAP_DAY, tz_name=MADRID,
                           dst_ambiguity_policy="run_all")
    assert nxt == datetime(2026, 3, 29, 1, 30)


# ---------------------------------------------------------------------------
# compute_next_run — fall-back repeat (2026-10-25 02:30 happens twice)
# ---------------------------------------------------------------------------

def test_compute_next_run_ambiguous_skip_advances_past_the_repeat_day():
    nxt = compute_next_run("daily", "02:30", after=FOLD_DAY, tz_name=MADRID,
                           dst_ambiguity_policy="skip")
    assert nxt == datetime(2026, 10, 26, 1, 30)


def test_compute_next_run_ambiguous_run_once_fires_the_earlier_instant():
    nxt = compute_next_run("daily", "02:30", after=FOLD_DAY, tz_name=MADRID,
                           dst_ambiguity_policy="run_once")
    assert nxt == datetime(2026, 10, 25, 0, 30)


def test_compute_next_run_cron_also_resolves_dst_with_croniter():
    """AUTO-02/QA-43 applies to cron schedules too, not just daily/weekly."""
    nxt = compute_next_run("cron", None, after=FOLD_DAY, cron_expression="30 2 * * *",
                           tz_name=MADRID, dst_ambiguity_policy="run_once")
    assert nxt == datetime(2026, 10, 25, 0, 30)


# ---------------------------------------------------------------------------
# run_all's second occurrence, via TaskScheduler._next_moment_after
# ---------------------------------------------------------------------------

def test_run_all_queues_the_fold1_followup_one_hour_later():
    tid = f"auto02-run-all-{uuid.uuid4()}"
    set_task_policy(tid, dst_ambiguity_policy="run_all")
    task = SimpleTask(id=tid, schedule="daily", scheduled_time="02:30", timezone=MADRID)
    sched = TaskScheduler(session_manager=None)

    first = sched._next_moment_after(None, task, after=FOLD_DAY)
    assert first == datetime(2026, 10, 25, 0, 30), "first occurrence: fold=0, earlier instant"

    second = sched._next_moment_after(None, task, after=FOLD_DAY)
    assert second == datetime(2026, 10, 25, 1, 30), "second occurrence: fold=1, one hour later"

    # The pending follow-up is consumed exactly once — a third call recomputes
    # a fresh cycle instead of replaying the same repeat forever.
    third = sched._next_moment_after(None, task, after=FOLD_DAY)
    assert third == first, "no more pending follow-up: recomputes from `after` again"


def test_run_once_never_queues_a_followup():
    tid = f"auto02-run-once-{uuid.uuid4()}"
    set_task_policy(tid, dst_ambiguity_policy="run_once")
    task = SimpleTask(id=tid, schedule="daily", scheduled_time="02:30", timezone=MADRID)
    sched = TaskScheduler(session_manager=None)
    first = sched._next_moment_after(None, task, after=FOLD_DAY)
    second = sched._next_moment_after(None, task, after=first)
    assert first == datetime(2026, 10, 25, 0, 30)
    # No pending fold=1 was queued — the second call is just the NEXT day,
    # already back to CET (UTC+1) since the fall-back happened in between.
    assert second == datetime(2026, 10, 26, 1, 30)


# ---------------------------------------------------------------------------
# Policy validation / round trip
# ---------------------------------------------------------------------------

def test_set_task_policy_rejects_unknown_values():
    tid = f"auto02-validate-{uuid.uuid4()}"
    with pytest.raises(ValueError):
        set_task_policy(tid, dst_ambiguity_policy="whenever")
    with pytest.raises(ValueError):
        set_task_policy(tid, misfire_policy="whenever")
    with pytest.raises(ValueError):
        set_task_policy(tid, budget_preset="unlimited")


def test_get_task_policy_defaults_for_an_undeclared_task():
    tid = f"auto02-undeclared-{uuid.uuid4()}"
    policy = get_task_policy(tid)
    assert policy == {
        "dst_ambiguity_policy": "run_once",
        "misfire_policy": "fire_immediately",
        "budget_preset": "supervised",
        "permissions": None,
    }


def test_set_task_policy_round_trips_and_partial_updates_preserve_the_rest():
    tid = f"auto02-roundtrip-{uuid.uuid4()}"
    set_task_policy(tid, dst_ambiguity_policy="skip", misfire_policy="skip",
                    budget_preset="read_only", permissions=["bash", "web_search"])
    full = get_task_policy(tid)
    assert full == {
        "dst_ambiguity_policy": "skip", "misfire_policy": "skip",
        "budget_preset": "read_only", "permissions": ["bash", "web_search"],
    }
    # Updating just one field leaves the others exactly as they were.
    set_task_policy(tid, misfire_policy="fire_immediately")
    after = get_task_policy(tid)
    assert after["misfire_policy"] == "fire_immediately"
    assert after["dst_ambiguity_policy"] == "skip"
    assert after["budget_preset"] == "read_only"
    assert after["permissions"] == ["bash", "web_search"]


# ---------------------------------------------------------------------------
# Startup misfire sweep — machine off for 3 hours, real ScheduledTask row.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _shared_test_db(monkeypatch):
    """Same cross-thread fix as tests/test_conn_02_email_send_routes.py —
    not strictly needed here (no TestClient thread hop) but keeps every
    scheduler test in this file on one isolated in-memory database instead of
    the real on-disk one `DATABASE_URL` would otherwise point at."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal",
                        sessionmaker(autocommit=False, autoflush=False, bind=engine))
    db_mod.Base.metadata.create_all(bind=engine)
    yield


def _make_overdue_task(db_session, *, task_id, misfire_policy):
    from core.database import ScheduledTask
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    set_task_policy(task_id, misfire_policy=misfire_policy)
    row = ScheduledTask(
        id=task_id, owner="u1", name="hourly digest", task_type="action",
        action="noop", schedule="cron", cron_expression="0 * * * *",
        timezone=MADRID, status="active",
        next_run=now - timedelta(hours=3),  # 3h overdue
        trigger_type="schedule",
    )
    db_session.add(row)
    db_session.commit()
    return row


async def test_misfire_skip_abandons_the_missed_occurrences_and_jumps_to_the_future(monkeypatch):
    from core.database import SessionLocal, ScheduledTask

    monkeypatch.setenv("ODYSSEUS_INPROCESS_POLLERS", "0")
    db = SessionLocal()
    try:
        tid = f"auto02-misfire-skip-{uuid.uuid4()}"
        row = _make_overdue_task(db, task_id=tid, misfire_policy="skip")
        missed_next_run = row.next_run
    finally:
        db.close()

    sched = TaskScheduler(session_manager=None)
    try:
        await sched.start()
        db = SessionLocal()
        try:
            refreshed = db.query(ScheduledTask).filter(ScheduledTask.id == tid).first()
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            assert refreshed.next_run > now, "advanced to a FUTURE occurrence, not left overdue"
            assert refreshed.next_run != missed_next_run
            # skip abandons the missed run entirely — the new next_run is not
            # "fire soon" (fire_immediately's ~60s push); it's a real future
            # cron occurrence, which for an hourly cron from `now` is at most
            # one hour out.
            assert refreshed.next_run <= now + timedelta(hours=1, minutes=1)
        finally:
            db.close()
    finally:
        await sched.stop()


async def test_misfire_fire_immediately_is_the_unchanged_default(monkeypatch):
    from core.database import SessionLocal, ScheduledTask

    monkeypatch.setenv("ODYSSEUS_INPROCESS_POLLERS", "0")
    db = SessionLocal()
    try:
        tid = f"auto02-misfire-default-{uuid.uuid4()}"
        _make_overdue_task(db, task_id=tid, misfire_policy="fire_immediately")
    finally:
        db.close()

    sched = TaskScheduler(session_manager=None)
    try:
        await sched.start()
        db = SessionLocal()
        try:
            refreshed = db.query(ScheduledTask).filter(ScheduledTask.id == tid).first()
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            # Historical behavior preserved: pushed to fire shortly after startup.
            assert refreshed.next_run <= now + timedelta(seconds=90)
        finally:
            db.close()
    finally:
        await sched.stop()


class SimpleTask:
    """Minimal stand-in for a `ScheduledTask` ORM row — only the attributes
    `_next_moment_after`/`compute_next_run` actually read."""

    def __init__(self, *, id, schedule, scheduled_time=None, scheduled_day=None,
                scheduled_date=None, cron_expression=None, timezone=None,
                crew_member_id=None):
        self.id = id
        self.schedule = schedule
        self.scheduled_time = scheduled_time
        self.scheduled_day = scheduled_day
        self.scheduled_date = scheduled_date
        self.cron_expression = cron_expression
        self.timezone = timezone
        self.crew_member_id = crew_member_id
