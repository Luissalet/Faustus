"""A24 — acceptance-parity case.

Trigger (contrato, literal): "DST boundary missed schedule and duplicate
scheduler delivery".
Expected (contrato, literal): "Documented timezone misfire and
deduplication policy enforced".

Three mechanisms, all in `src/task_scheduler.py`, exercised here with a
real SQLite file (never a mock of the module under test) and a frozen
clock around the two 2026 Europe/Madrid DST transitions:

1. `compute_next_run(..., tz_name=...)` resolves a recurrence's next fire
   under an explicit, per-task `dst_ambiguity_policy` (see
   `DST_AMBIGUITY_POLICIES`/`_dst_resolve`) rather than letting `zoneinfo`
   pick silently — the nonexistent wall-clock reading on the spring-forward
   day (2026-03-29 02:00→03:00) fires once, at the first valid instant
   after the jump; the ambiguous reading on the fall-back day
   (2026-10-25 03:00→02:00) fires once, at its FIRST occurrence.
   Pre-existing, exhaustive coverage of this half lives in
   `tests/qa/test_qa_43_cambio_horario.py` and
   `tests/test_auto_02_task_dst_misfire.py`; this file adds the DST-boundary
   scenarios closest to A24's literal trigger text rather than repeating
   that suite.

2. `TaskScheduler.start()`'s misfire sweep applies a per-task
   `misfire_policy` (`fire_once`/`fire_immediately` — the same thing, two
   names, see `_parse_misfire_policy` — `skip`, or `catch_up_max:k`, all
   new/extended for A24 except the first two) to a task whose `next_run`
   fell in the past while nothing was watching.

3. `claim_fire_key`/`_claim_fire_key_literal` (A24, new) dedupe an
   occurrence's `fire_key = task_id + scheduled_at` (`occurrence_key`,
   aliased as `fire_key`) via a `PRIMARY KEY` on `scheduler_fire_log` — a
   second worker or retry that reaches the SAME occurrence's dispatch point
   twice claims it once and is refused the second time, backed by a real
   two-thread race, not a sequential re-check.
"""
from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timedelta, timezone

import pytest
from zoneinfo import ZoneInfo

from tests.acceptance.conftest import record_evidence
from tests.test_scheduler_lease_claim import add_task, db, make_scheduler, row  # noqa: F401

from src.task_scheduler import (
    DEFAULT_MISFIRE_POLICY,
    MISFIRE_POLICIES,
    _claim_fire_key_literal,
    _parse_misfire_policy,
    claim_fire_key,
    compute_next_run,
    fire_key,
    get_task_policy,
    occurrence_key,
    set_task_policy,
)

MADRID = ZoneInfo("Europe/Madrid")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ── 1. the DST boundary itself ──────────────────────────────────────────

@pytest.mark.acceptance("A24")
def test_spring_forward_gap_fires_once_at_the_first_valid_instant(request):
    """2026-03-29 02:00 CET -> 03:00 CEST: 02:30 never happens. `run_once`
    (the default) fires at the first valid local instant after the jump —
    once, not zero, not twice."""
    just_before = datetime(2026, 3, 29, 1, 0, tzinfo=MADRID)
    nxt = compute_next_run(
        "daily", "02:30", after=just_before.astimezone(timezone.utc).replace(tzinfo=None),
        tz_name="Europe/Madrid", dst_ambiguity_policy="run_once",
    )
    assert nxt is not None
    local = nxt.replace(tzinfo=timezone.utc).astimezone(MADRID)
    # 02:30 CET extrapolated across the gap lands at 03:30 CEST — the
    # earliest honest instant "02:30" can be said to have arrived.
    assert (local.month, local.day, local.hour, local.minute) == (3, 29, 3, 30)
    assert local.utcoffset() == timedelta(hours=2)  # CEST — after the jump

    # `skip` abandons this occurrence entirely: the next candidate is a day
    # later, a real (non-gap) 02:30.
    nxt_skip = compute_next_run(
        "daily", "02:30", after=just_before.astimezone(timezone.utc).replace(tzinfo=None),
        tz_name="Europe/Madrid", dst_ambiguity_policy="skip",
    )
    local_skip = nxt_skip.replace(tzinfo=timezone.utc).astimezone(MADRID)
    assert (local_skip.month, local_skip.day, local_skip.hour, local_skip.minute) == (3, 30, 2, 30)
    record_evidence(request, mechanism="compute_next_run/_dst_resolve",
                     scenario="2026-03-29 spring-forward gap",
                     run_once_utc=nxt.isoformat(), skip_utc=nxt_skip.isoformat())


@pytest.mark.acceptance("A24")
def test_fall_back_repeat_fires_exactly_once_at_the_first_occurrence(request):
    """2026-10-25 03:00 CEST -> 02:00 CET: 02:30 happens twice, an hour
    apart. `run_once` fires exactly once, at the EARLIER (fold=0, CEST)
    occurrence — not the later one, and never both."""
    just_before = datetime(2026, 10, 25, 1, 0, tzinfo=MADRID)
    nxt = compute_next_run(
        "daily", "02:30", after=just_before.astimezone(timezone.utc).replace(tzinfo=None),
        tz_name="Europe/Madrid", dst_ambiguity_policy="run_once",
    )
    assert nxt is not None
    local = nxt.replace(tzinfo=timezone.utc).astimezone(MADRID)
    assert (local.month, local.day, local.hour, local.minute) == (10, 25, 2, 30)
    assert local.utcoffset() == timedelta(hours=2)  # CEST — the FIRST 02:30

    # The next call, seeded strictly after this fire, must not re-offer the
    # same ambiguous wall-clock reading a second time under run_once — it
    # advances a full day forward instead of finding the second (fold=1)
    # 02:30 an hour later.
    again = compute_next_run(
        "daily", "02:30", after=nxt, tz_name="Europe/Madrid", dst_ambiguity_policy="run_once",
    )
    local_again = again.replace(tzinfo=timezone.utc).astimezone(MADRID)
    assert (local_again.month, local_again.day) == (10, 26)
    record_evidence(request, mechanism="compute_next_run/_dst_resolve",
                     scenario="2026-10-25 fall-back repeat",
                     first_fire_utc=nxt.isoformat(), next_fire_utc=again.isoformat())


# ── 2. misfire policy, applied by the startup sweep ─────────────────────

@pytest.mark.acceptance("A24")
def test_misfire_policy_is_declared_per_task_and_validated(request):
    """The contract's exact vocabulary (`fire_once`/`skip`/`catch_up_max:k`)
    round-trips through `set_task_policy`/`get_task_policy`, and an invalid
    value (no colon, non-numeric or zero/negative `k`) is rejected rather
    than silently accepted."""
    undeclared = get_task_policy("a24-undeclared")
    assert undeclared["misfire_policy"] == DEFAULT_MISFIRE_POLICY
    assert _parse_misfire_policy(DEFAULT_MISFIRE_POLICY) == ("fire_once", None)

    set_task_policy("a24-fire-once", misfire_policy="fire_once")
    assert get_task_policy("a24-fire-once")["misfire_policy"] == "fire_once"
    assert _parse_misfire_policy("fire_once") == ("fire_once", None)
    assert _parse_misfire_policy("fire_immediately") == ("fire_once", None)

    set_task_policy("a24-skip", misfire_policy="skip")
    assert _parse_misfire_policy(get_task_policy("a24-skip")["misfire_policy"]) == ("skip", None)

    set_task_policy("a24-catchup", misfire_policy="catch_up_max:3")
    assert get_task_policy("a24-catchup")["misfire_policy"] == "catch_up_max:3"
    assert _parse_misfire_policy("catch_up_max:3") == ("catch_up_max", 3)

    for bad in ("catch_up_max:0", "catch_up_max:-1", "catch_up_max:abc", "catch_up_max", "bogus"):
        with pytest.raises(ValueError):
            set_task_policy("a24-bad", misfire_policy=bad)
    record_evidence(request, mechanism="set_task_policy/get_task_policy/_parse_misfire_policy",
                     policies=list(MISFIRE_POLICIES) + ["catch_up_max:<k>"])


@pytest.mark.acceptance("A24")
@pytest.mark.asyncio
async def test_catch_up_max_runs_at_most_k_missed_occurrences(db, monkeypatch, request):  # noqa: F811
    """A daily 09:00 task, offline for 5 days, with `catch_up_max:2`: the
    startup sweep must queue exactly 2 of the 5 missed occurrences (the two
    OLDEST — oldest debt first) and abandon the rest, never all 5 and never
    zero."""
    import src.task_scheduler as ts_mod

    frozen = {"now": datetime(2026, 6, 10, 9, 5, 0)}
    monkeypatch.setattr(ts_mod, "_utcnow", lambda: frozen["now"])

    add_task(db, "t_catchup", due=datetime(2026, 6, 5, 9, 0, 0),
              schedule="daily", scheduled_time="09:00")
    ts_mod.set_task_policy("t_catchup", misfire_policy="catch_up_max:2")

    sch = make_scheduler()
    sch._session_manager = None
    await sch.start()
    try:
        first = row(db, "t_catchup")
        # The oldest missed occurrence (2026-06-05 09:00) is dispatched
        # first, unmodified — a catch-up run keeps its ORIGINAL scheduled
        # instant (and so its own fire_key), not "now".
        assert first.next_run == datetime(2026, 6, 5, 9, 0, 0)

        # One catch-up instant is still owed (2026-06-06 09:00) — draining
        # it via _next_moment_after must return that exact instant before
        # falling through to the live daily schedule.
        second = sch._next_moment_after(db.SessionLocal(), first, after=frozen["now"])
        assert second == datetime(2026, 6, 6, 9, 0, 0)

        # No more debt owed: the NEXT call resumes the live schedule at (or
        # after) "now", not 06-07/06-08/06-09 (those were abandoned by the
        # k=2 cap) and not a third catch-up instant.
        third = sch._next_moment_after(db.SessionLocal(), first, after=frozen["now"])
        assert third is not None and third >= frozen["now"]
        assert third not in (datetime(2026, 6, 7, 9, 0, 0), datetime(2026, 6, 8, 9, 0, 0))
    finally:
        await sch.stop()
    record_evidence(request, mechanism="TaskScheduler.start (misfire sweep) + "
                                       "_enumerate_missed_occurrences + catchup_pending queue",
                     task_id="t_catchup", first_catchup=str(first.next_run),
                     second_catchup=str(second), resumed_live=str(third))


# ── 3. fire_key deduplication ────────────────────────────────────────────

@pytest.mark.acceptance("A24")
def test_fire_key_is_task_id_plus_scheduled_at(request):
    due = datetime(2026, 6, 5, 9, 0, 0)
    assert fire_key("t1", due) == occurrence_key("t1", due) == "task:t1:2026-06-05T09:00:00"
    record_evidence(request, mechanism="fire_key/occurrence_key", example=fire_key("t1", due))


@pytest.mark.acceptance("A24")
def test_two_concurrent_claims_of_the_same_fire_key_dedupe(request):
    """The contract's own scenario: two claims racing on the SAME fire_key,
    genuinely concurrent (a `threading.Barrier`, not a sequential call),
    enforced by the `scheduler_fire_log.fire_key` PRIMARY KEY — exactly one
    of the two must win."""
    # A unique task id per run: `claim_fire_key`'s table lives in the real,
    # persistent `task_policies.db` (same store `set_task_policy` uses) —
    # not test-isolated — so a fixed literal key would collide with a
    # previous run of this very test and always read back "already claimed".
    import uuid
    task_id = f"t_race_{uuid.uuid4().hex}"
    barrier = threading.Barrier(2, timeout=10)
    results = []
    lock = threading.Lock()

    def attempt():
        barrier.wait()
        ok = claim_fire_key(task_id, datetime(2026, 6, 5, 9, 0, 0))
        with lock:
            results.append(ok)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert sorted(results) == [False, True]
    record_evidence(request, mechanism="claim_fire_key + scheduler_fire_log PRIMARY KEY",
                     results=results)


@pytest.mark.acceptance("A24")
def test_a_literal_fire_key_claim_is_also_exactly_once(request):
    """`_claim_fire_key_literal` — what `_execute_task_locked` actually
    calls with `task.lease_key` — is the same PRIMARY-KEY-backed gate as
    `claim_fire_key`, just given an already-formed key instead of a
    `(task_id, due_at)` pair."""
    import uuid
    task_id = f"t_lit_{uuid.uuid4().hex}"
    key = f"task:{task_id}:2026-06-05T09:00:00"
    assert _claim_fire_key_literal(key, task_id, None) is True
    assert _claim_fire_key_literal(key, task_id, None) is False
    record_evidence(request, mechanism="_claim_fire_key_literal", key=key)


@pytest.mark.acceptance("A24")
@pytest.mark.asyncio
async def test_a_retry_after_lease_recovery_still_executes_once_not_zero_times(db, monkeypatch, request):  # noqa: F811
    """The two mechanisms cooperate rather than contradict each other:
    `_claim_due_task` allows a legitimate retry to re-claim a recovered
    lease (same `due_at`/`lease_key`, attempt 2) — it must NOT be treated as
    a duplicate at the CLAIM step — while the fire_key gate in
    `_execute_task_locked` still guarantees the occurrence's EFFECT only
    ever fires once. This is the regression this file guards against: an
    earlier draft of A24 wired `claim_fire_key` into `_claim_due_task`
    itself and broke exactly this retry (see
    `tests/test_scheduler_lease_claim.py::
    test_a_recovered_lease_keeps_the_occurrence_key_and_counts_the_attempt`).
    """
    from datetime import timedelta as _td

    import src.task_scheduler as ts_mod

    now = _utcnow()
    due = now - _td(minutes=1)
    add_task(db, "t_retry", due=due, target="session")

    sch = make_scheduler()
    assert sch._claim_due_task("t_retry", due, now) is True
    claimed_row = row(db, "t_retry")
    assert claimed_row.lease_key is not None

    # Attempt 1 never reached the effect (simulated crash) — its lease
    # expires and is recovered.
    session = db.SessionLocal()
    try:
        session.query(db.ScheduledTask).filter(db.ScheduledTask.id == "t_retry").update(
            {"lease_expires_at": _utcnow() - _td(seconds=1)})
        session.commit()
    finally:
        session.close()
    recovered = sch._recover_expired_leases()
    assert [r["action"] for r in recovered] == ["released"]

    # Attempt 2 re-claims the SAME occurrence — must succeed at the claim
    # step (this is the retry, not a duplicate).
    assert sch._claim_due_task("t_retry", due, _utcnow()) is True
    retried_row = row(db, "t_retry")
    assert retried_row.lease_key == claimed_row.lease_key  # same occurrence

    # Attempt 2 reaches the effect and claims the fire_key — succeeds, this
    # is genuinely the first effect for this occurrence.
    assert ts_mod._claim_fire_key_literal(retried_row.lease_key, "t_retry", None) is True
    # A THIRD attempt on the same occurrence, after the effect already
    # happened, must be refused — this is the actual duplicate.
    assert ts_mod._claim_fire_key_literal(retried_row.lease_key, "t_retry", None) is False
    record_evidence(request, mechanism="_claim_due_task retry + _claim_fire_key_literal dedup",
                     lease_key=retried_row.lease_key)
