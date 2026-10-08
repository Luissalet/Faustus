"""The skill sleep pass on a schedule (src/skills_runtime/sleep_schedule.py).

A fake clock and a fake model call drive the real scheduler: nothing here
loads a model or touches the real data directory. Covers: off by default, the
hour boundary, once per day, restart persistence, overlap with an on-demand
run, the catch-up window, a busy machine (skip now, retry later, the day is
not used up), model trouble retried a bounded number of times, candidate
selection, shutdown giving the day back, the loop surviving errors and the
status the API serves.
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta

import pytest

from src.settings import DEFAULT_SETTINGS
from src.skills_runtime import sleep_optimize as sp
from src.skills_runtime import sleep_schedule as ss


# -- fixtures ---------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    root = tmp_path / "skill_proposals"
    monkeypatch.setattr(sp, "PROPOSALS_ROOT", str(root))
    monkeypatch.setattr(sp, "PROPOSALS_FILE", str(root / "proposals.json"))
    monkeypatch.setattr(sp, "SNAPSHOT_DIR", str(root / "snapshots"))
    ss.reset_for_tests()
    yield
    ss.reset_for_tests()


@pytest.fixture
def cfg(monkeypatch):
    values = {ss.SETTING_ENABLED: True, ss.SETTING_HOUR: 3}
    monkeypatch.setattr(ss, "_get_setting", lambda key, default=None: values.get(key, default))
    return values


class Clock:
    def __init__(self, at: datetime):
        self.at = at

    def __call__(self) -> datetime:
        return self.at

    def to(self, text: str) -> "Clock":
        self.at = datetime.fromisoformat(text)
        return self


def _evidence(n_neg=1, n_ok=1):
    items = [{"user_reaction": "negative", "tool_errors": [], "id": f"n{i}"} for i in range(n_neg)]
    items += [{"user_reaction": "positive", "tool_errors": [], "id": f"p{i}"} for i in range(n_ok)]
    return items


class Rig:
    """Fake skills + fake evidence + fake model + a controllable busy probe."""

    def __init__(self, clock: Clock):
        self.clock = clock
        self.skills = [{"name": "alpha", "owner": "luis", "status": "published"}]
        self.evidence = {"alpha": _evidence()}
        self.blocked: str | None = None
        self.proposed: list[str] = []
        self.propose_error: Exception | None = None
        self.seen_active: list[list[dict]] = []

    def deps(self) -> ss.Deps:
        async def propose(skill_id, evidence, *, url=None, model=None, owner=None):
            self.seen_active.append(ss.active_runs())
            if self.propose_error:
                raise self.propose_error
            self.proposed.append(skill_id)
            rec = {"id": f"prop-{len(self.proposed)}", "skill_id": skill_id,
                   "owner": owner or "", "status": "pending", "created_at": 1.0}
            return rec

        return ss.Deps(
            now=self.clock,
            skills=lambda: self.skills,
            evidence=lambda name, **kw: list(self.evidence.get(name, [])),
            propose=propose,
            blocker=lambda **kw: self.blocked,
            endpoint=lambda: ("http://stub/v1/chat/completions", "stub-model", {}),
            in_thread=False,
        )


def run(coro):
    return asyncio.run(coro)


# -- settings ---------------------------------------------------------------

def test_disabled_by_default_and_hour_defaults():
    assert DEFAULT_SETTINGS["skills_sleep_pass_enabled"] is False
    assert DEFAULT_SETTINGS["skills_sleep_pass_hour"] == 3


@pytest.mark.parametrize("raw,expected", [(3, 3), (0, 0), (23, 23), ("7", 7), (24, 3), (-1, 3), ("x", 3), (None, 3)])
def test_configured_hour_is_clamped_and_midnight_is_valid(monkeypatch, raw, expected):
    monkeypatch.setattr(ss, "_get_setting", lambda key, default=None: raw)
    assert ss.configured_hour() == expected


def test_disabled_does_nothing_and_writes_nothing(cfg):
    cfg[ss.SETTING_ENABLED] = False
    rig = Rig(Clock(datetime(2026, 10, 8, 3, 0)))
    out = run(ss.tick(deps=rig.deps()))
    assert out == {"status": "skipped", "reason": "disabled"}
    assert rig.proposed == []
    assert not os.path.exists(ss._state_path())


# -- the clock --------------------------------------------------------------

@pytest.mark.parametrize("now,due", [
    ("2026-10-08T02:59:59", False),   # before the hour: yesterday's slot, long closed
    ("2026-10-08T03:00:00", True),   # the hour itself
    ("2026-10-08T05:59:59", True),   # last second of the window
    ("2026-10-08T06:00:00", False),  # window closed
])
def test_hour_boundary_and_window(now, due):
    d = ss.evaluate(datetime.fromisoformat(now), enabled=True, hour=3, last_run_date=None)
    assert (d.action == "run") is due
    if not due:
        assert d.reason == "outside_window"


def test_window_crosses_midnight_with_the_slot_date_of_the_evening():
    d = ss.evaluate(datetime(2026, 10, 9, 1, 0), enabled=True, hour=23, last_run_date=None)
    assert d.action == "run" and d.slot_date == "2026-10-08"
    again = ss.evaluate(datetime(2026, 10, 9, 1, 0), enabled=True, hour=23, last_run_date="2026-10-08")
    assert again.action == "skip" and again.reason == "already_ran"


# -- once per day, restart, overlap, catch-up -------------------------------

def test_runs_once_per_day_then_again_the_next_day(cfg):
    clock = Clock(datetime(2026, 10, 8, 3, 0))
    rig = Rig(clock)
    first = run(ss.tick(deps=rig.deps()))
    assert first["status"] == "ran" and first["reason"] == "done"
    assert rig.proposed == ["alpha"]

    clock.to("2026-10-08T03:05:00")
    assert run(ss.tick(deps=rig.deps())) == {"status": "skipped", "reason": "already_ran"}
    clock.to("2026-10-08T05:30:00")
    assert run(ss.tick(deps=rig.deps()))["reason"] == "already_ran"
    assert rig.proposed == ["alpha"]

    # The skill got a proposal tonight; the cooldown keeps tomorrow's pass off it,
    # so give the next day a different skill to prove the day itself re-opens.
    rig.skills.append({"name": "beta", "owner": "luis", "status": "published"})
    rig.evidence["beta"] = _evidence()
    clock.to("2026-10-09T03:00:00")
    sp.save_proposal({"id": "old", "skill_id": "alpha", "owner": "luis", "status": "pending",
                      "created_at": clock().timestamp()})
    assert run(ss.tick(deps=rig.deps()))["status"] == "ran"
    assert rig.proposed == ["alpha", "beta"]


def test_day_is_claimed_before_the_work_starts_and_survives_a_restart(cfg):
    clock = Clock(datetime(2026, 10, 8, 3, 0))
    rig = Rig(clock)
    seen = {}

    async def spying_propose(skill_id, evidence, **kw):
        seen["state"] = ss.load_state()          # read from disk while the pass runs
        raise RuntimeError("process dies here")

    deps = rig.deps()
    deps.propose = spying_propose
    run(ss.tick(deps=deps))
    assert seen["state"]["last_run_date"] == "2026-10-08"
    assert "running_since" in seen["state"]

    # "Restart": nothing in memory survives, only the file.
    ss.reset_for_tests()
    clock.to("2026-10-08T03:10:00")
    fresh = Rig(clock)
    out = run(ss.tick(deps=fresh.deps()))
    assert out["reason"] == "already_ran" or out["status"] == "ran"   # a crash is retried at most MAX_ATTEMPTS
    assert json.load(open(ss._state_path(), encoding="utf-8"))["last_run_date"] == "2026-10-08"


def test_restart_after_a_finished_run_does_not_repeat_the_day(cfg):
    clock = Clock(datetime(2026, 10, 8, 3, 0))
    run(ss.tick(deps=Rig(clock).deps()))
    ss.reset_for_tests()
    clock.to("2026-10-08T04:00:00")
    other = Rig(clock)
    assert run(ss.tick(deps=other.deps())) == {"status": "skipped", "reason": "already_ran"}
    assert other.proposed == []


def test_does_not_overlap_an_on_demand_run_and_does_not_burn_the_day(cfg):
    clock = Clock(datetime(2026, 10, 8, 3, 0))
    rig = Rig(clock)
    deps = rig.deps()
    deps.blocker = ss.admission_blocker          # the real one: overlap is its first check
    with ss.track("manual"):
        out = run(ss.tick(deps=deps))
    assert out == {"status": "deferred", "reason": "sleep_pass_running"}
    assert rig.proposed == []
    assert ss.load_state().get("last_run_date") is None

    clock.to("2026-10-08T03:05:00")
    deps.blocker = lambda **kw: None
    assert run(ss.tick(deps=deps))["status"] == "ran"


def test_a_scheduled_run_is_visible_to_the_overlap_guard_while_it_runs(cfg):
    rig = Rig(Clock(datetime(2026, 10, 8, 3, 0)))
    run(ss.tick(deps=rig.deps()))
    assert rig.seen_active and rig.seen_active[0][0]["label"] == "scheduled"
    assert ss.active_runs() == []          # released afterwards


def test_machine_asleep_at_the_hour_catches_up_inside_the_window(cfg):
    clock = Clock(datetime(2026, 10, 8, 4, 45))      # woke up 1h45 late
    rig = Rig(clock)
    assert run(ss.tick(deps=rig.deps()))["status"] == "ran"
    assert ss.load_state()["last_run_date"] == "2026-10-08"


def test_machine_asleep_past_the_window_waits_for_the_next_day(cfg):
    clock = Clock(datetime(2026, 10, 8, 7, 0))       # 4h late: window (3h) closed
    rig = Rig(clock)
    out = run(ss.tick(deps=rig.deps()))
    assert out == {"status": "skipped", "reason": "outside_window"}
    assert rig.proposed == []
    state = ss.load_state()
    assert state["last_missed_date"] == "2026-10-08" and "last_run_date" not in state
    run(ss.tick(deps=rig.deps()))                     # idempotent: no second write needed
    clock.to("2026-10-09T03:00:00")
    assert run(ss.tick(deps=rig.deps()))["status"] == "ran"


# -- admission --------------------------------------------------------------

def test_busy_machine_skips_now_and_retries_later_without_using_the_day(cfg):
    clock = Clock(datetime(2026, 10, 8, 3, 0))
    rig = Rig(clock)
    rig.blocked = "lease_sibling_active"
    out = run(ss.tick(deps=rig.deps()))
    assert out == {"status": "deferred", "reason": "lease_sibling_active"}
    assert rig.proposed == []
    state = ss.load_state()
    assert state["last_deferred"]["reason"] == "lease_sibling_active"
    assert state.get("last_run_date") is None

    clock.to("2026-10-08T03:30:00")
    rig.blocked = None
    out = run(ss.tick(deps=rig.deps()))
    assert out["status"] == "ran" and rig.proposed == ["alpha"]
    assert "last_deferred" not in ss.load_state()


def test_a_machine_busy_for_the_whole_window_misses_the_day(cfg):
    clock = Clock(datetime(2026, 10, 8, 3, 0))
    rig = Rig(clock)
    rig.blocked = "foreground_activity"
    for stamp in ("03:00", "04:00", "05:55"):
        clock.to(f"2026-10-08T{stamp}:00")
        assert run(ss.tick(deps=rig.deps()))["status"] == "deferred"
    clock.to("2026-10-08T06:05:00")
    assert run(ss.tick(deps=rig.deps()))["reason"] == "outside_window"
    assert rig.proposed == []


def test_busy_check_runs_again_between_skills_and_stops_a_pass_midway(cfg):
    clock = Clock(datetime(2026, 10, 8, 3, 0))
    rig = Rig(clock)
    rig.skills.append({"name": "beta", "owner": "luis", "status": "published"})
    rig.evidence["beta"] = _evidence(n_neg=3)
    probes = []

    def blocker(**kw):
        probes.append(kw)
        return None if len(probes) == 1 else "foreground_activity"   # free at the start, busy after the first skill

    deps = rig.deps()
    deps.blocker = blocker
    out = run(ss.tick(deps=deps))
    assert out["status"] == "ran" and out["reason"] == "partial"
    assert rig.proposed == ["beta"]                                  # highest score first, then stopped
    assert out["result"]["stopped"] == "foreground_activity"
    assert ss.load_state()["last_run_date"] == "2026-10-08"          # a partial pass keeps the day


def test_admission_blocker_reasons_in_order(monkeypatch):
    from src import background_job_guard as guard, interactive_gate, model_lease, unattended_breaker, vram_admission

    url, model = "http://127.0.0.1:11434/v1/chat/completions", "m"
    monkeypatch.setattr(unattended_breaker, "is_open", lambda *a, **k: False)
    monkeypatch.setattr(interactive_gate, "has_foreground_activity", lambda *a, **k: False)
    monkeypatch.setattr(guard, "should_run_with_model", lambda *a, **k: True)
    monkeypatch.setattr(guard, "model_busy", lambda u: False)
    monkeypatch.setattr(guard, "no_free_slot", lambda u: False)
    monkeypatch.setattr(model_lease, "enabled", lambda: True)
    monkeypatch.setattr(model_lease, "sibling_reserved_bytes", lambda root: 0)
    monkeypatch.setattr(model_lease, "sibling_active", lambda root: {})
    monkeypatch.setattr(vram_admission, "ollama_root", lambda u: "http://127.0.0.1:11434")
    assert ss.admission_blocker(url=url, model=model) is None

    monkeypatch.setattr(model_lease, "sibling_active", lambda root: {"other": __import__("time").time() - 5})
    assert ss.admission_blocker(url=url, model=model) == "lease_sibling_active"
    monkeypatch.setattr(model_lease, "sibling_active", lambda root: {"other": __import__("time").time() - 9999})
    assert ss.admission_blocker(url=url, model=model) is None
    monkeypatch.setattr(model_lease, "sibling_reserved_bytes", lambda root: 4_000_000_000)
    assert ss.admission_blocker(url=url, model=model) == "lease_reserved"
    monkeypatch.setattr(model_lease, "sibling_reserved_bytes", lambda root: 0)

    monkeypatch.setattr(guard, "model_busy", lambda u: True)
    assert ss.admission_blocker(url=url, model=model) == "model_busy"
    monkeypatch.setattr(guard, "model_busy", lambda u: False)
    monkeypatch.setattr(guard, "should_run_with_model", lambda *a, **k: False)
    assert ss.admission_blocker(url=url, model=model) == "would_load_model"
    monkeypatch.setattr(guard, "should_run_with_model", lambda *a, **k: True)
    monkeypatch.setattr(interactive_gate, "has_foreground_activity", lambda *a, **k: True)
    assert ss.admission_blocker(url=url, model=model) == "foreground_activity"
    monkeypatch.setattr(unattended_breaker, "is_open", lambda *a, **k: True)
    assert ss.admission_blocker(url=url, model=model) == "unattended_breaker_open"
    with ss.track("manual"):
        assert ss.admission_blocker(url=url, model=model) == "sleep_pass_running"


def test_no_endpoint_is_a_reason_not_a_model_load(monkeypatch):
    monkeypatch.setattr(ss, "_resolve_pass_endpoint", lambda: (None, None, None))
    from src import interactive_gate, unattended_breaker
    monkeypatch.setattr(unattended_breaker, "is_open", lambda *a, **k: False)
    monkeypatch.setattr(interactive_gate, "has_foreground_activity", lambda *a, **k: False)
    assert ss.admission_blocker() == "no_endpoint"


# -- what a pass does -------------------------------------------------------

def test_select_candidates_wants_failure_evidence_and_respects_the_cooldown():
    now = 1_000_000.0
    skills = [
        {"name": "fails", "owner": "luis", "status": "published"},
        {"name": "fine", "owner": "luis", "status": "published"},
        {"name": "thin", "owner": "luis", "status": "published"},
        {"name": "drafty", "owner": "luis", "status": "draft"},
        {"name": "recent", "owner": "luis", "status": "published"},
        {"name": "errs", "owner": "*", "status": "published"},
        {"name": "x1", "owner": "", "status": "published"},
        {"name": "x2", "owner": "", "status": "published"},
        {"name": "x3", "owner": "", "status": "published"},
    ]
    evidence = {
        "fails": _evidence(n_neg=2), "fine": _evidence(n_neg=0, n_ok=4), "thin": _evidence(n_neg=1, n_ok=0),
        "drafty": _evidence(n_neg=3), "recent": _evidence(n_neg=3),
        "errs": [{"user_reaction": "neutral", "tool_errors": ["boom"]}, {"user_reaction": "neutral", "tool_errors": []}],
        "x1": _evidence(n_neg=1), "x2": _evidence(n_neg=1), "x3": _evidence(n_neg=1),
    }
    proposals = {"p": {"skill_id": "recent", "owner": "luis", "created_at": now - 2 * 86400},
                 "old": {"skill_id": "fine", "owner": "luis", "created_at": now - 30 * 86400}}
    calls = []

    def ev(name, **kw):
        calls.append((name, kw.get("owner")))
        return evidence[name]

    picked = ss.select_candidates(skills, ev, proposals, now_ts=now)
    names = [c["skill_id"] for c in picked]
    assert names[0] == "fails" and len(names) == ss.MAX_SKILLS_PER_RUN
    assert "fine" not in names and "thin" not in names and "drafty" not in names and "recent" not in names
    assert ("errs", None) in calls            # the global-owner sentinel means "no owner filter"
    assert ("fails", "luis") in calls


def test_a_pass_only_proposes(cfg, monkeypatch):
    """No apply/approve path is reachable from the scheduler."""
    called = []
    monkeypatch.setattr(sp, "approve_proposal", lambda *a, **k: called.append("approve"))
    rig = Rig(Clock(datetime(2026, 10, 8, 3, 0)))
    out = run(ss.tick(deps=rig.deps()))
    assert out["result"]["proposals"] == [{"id": "prop-1", "skill_id": "alpha"}]
    assert called == []
    assert not hasattr(ss, "approve_proposal") and not hasattr(ss, "approve")
    assert sp.list_proposals_for(status="approved") == []


def test_nothing_worth_proposing_still_uses_the_day(cfg):
    rig = Rig(Clock(datetime(2026, 10, 8, 3, 0)))
    rig.evidence["alpha"] = _evidence(n_neg=0, n_ok=5)
    out = run(ss.tick(deps=rig.deps()))
    assert out["result"]["status"] == "nothing_to_do"
    assert rig.proposed == []
    assert run(ss.tick(deps=rig.deps()))["reason"] == "already_ran"


def test_model_trouble_is_retried_a_bounded_number_of_times(cfg):
    clock = Clock(datetime(2026, 10, 8, 3, 0))
    rig = Rig(clock)
    rig.propose_error = sp.SleepPassError("sleep_pass.model_call_failed", "down")
    outcomes = []
    for minute in (0, 5, 10, 15, 20):
        clock.to(f"2026-10-08T03:{minute:02d}:00")
        outcomes.append(run(ss.tick(deps=rig.deps())))
    assert [o["status"] for o in outcomes[:3]] == ["ran", "ran", "ran"]
    assert all(o["result"]["status"] == "failed" for o in outcomes[:3])
    assert outcomes[3] == {"status": "skipped", "reason": "already_ran"}      # MAX_ATTEMPTS reached
    assert outcomes[4]["reason"] == "already_ran"
    assert len(rig.seen_active) == ss.MAX_ATTEMPTS

    # ... and it works again the next day.
    rig.propose_error = None
    clock.to("2026-10-09T03:00:00")
    assert run(ss.tick(deps=rig.deps()))["result"]["status"] == "done"


def test_a_validation_refusal_is_not_retried(cfg):
    clock = Clock(datetime(2026, 10, 8, 3, 0))
    rig = Rig(clock)
    rig.propose_error = sp.SleepPassError("sleep_pass.grew_too_much", "too big")
    out = run(ss.tick(deps=rig.deps()))
    assert out["result"]["status"] == "failed"
    clock.to("2026-10-08T03:05:00")
    assert run(ss.tick(deps=rig.deps()))["reason"] == "already_ran"


def test_shutdown_in_the_middle_gives_the_day_back(cfg):
    clock = Clock(datetime(2026, 10, 8, 3, 0))
    rig = Rig(clock)
    deps = rig.deps()

    async def cancelled(*a, **k):
        raise asyncio.CancelledError()

    deps.propose = cancelled
    with pytest.raises(asyncio.CancelledError):
        run(ss.tick(deps=deps))
    assert ss.active_runs() == []
    assert ss.load_state().get("last_run_date") != "2026-10-08"
    clock.to("2026-10-08T03:10:00")
    assert run(ss.tick(deps=rig.deps()))["status"] == "ran"


# -- the loop and the status ------------------------------------------------

def test_loop_survives_a_failing_tick_and_stops_when_cancelled(cfg):
    clock_calls = []

    def exploding_now():
        clock_calls.append(1)
        raise RuntimeError("clock exploded")

    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) > 3:
            raise asyncio.CancelledError()

    deps = Rig(Clock(datetime(2026, 10, 8, 3, 0))).deps()
    deps.now = exploding_now
    with pytest.raises(asyncio.CancelledError):
        run(ss.scheduler_loop(None, tick_seconds=7, startup_delay=11, deps=deps, sleep=fake_sleep))
    assert sleeps[0] == 11 and sleeps[1:] == [7, 7, 7]
    assert len(clock_calls) == 3


def test_status_reports_next_run_and_last_result(cfg):
    clock = Clock(datetime(2026, 10, 8, 10, 0))
    off = dict(cfg)
    cfg[ss.SETTING_ENABLED] = False
    s = ss.status(clock())
    assert s["enabled"] is False and s["phase"] == "disabled" and s["next_run_at"] is None
    cfg.update(off)

    s = ss.status(clock())
    assert s["phase"] == "scheduled" and s["next_run_at"] == "2026-10-09T03:00:00" and s["hour"] == 3
    assert s["last_run"] is None

    clock.to("2026-10-09T03:00:00")
    assert ss.status(clock())["phase"] == "due"

    rig = Rig(clock)
    run(ss.tick(deps=rig.deps()))
    s = ss.status(clock())
    assert s["phase"] == "scheduled" and s["next_run_at"] == "2026-10-10T03:00:00"
    assert s["last_run_date"] == "2026-10-09"
    assert s["last_run"]["proposals"] == 1 and s["last_run"]["status"] == "done"
    assert "alpha" not in json.dumps(s)           # counts only, no skill names

    with ss.track("manual"):
        assert ss.status(clock())["phase"] == "running"


def test_status_shows_what_a_due_pass_is_waiting_for(cfg):
    clock = Clock(datetime(2026, 10, 8, 3, 0))
    rig = Rig(clock)
    rig.blocked = "foreground_activity"
    run(ss.tick(deps=rig.deps()))
    s = ss.status(clock())
    assert s["phase"] == "due" and s["waiting_for"] == "foreground_activity"
