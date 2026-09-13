"""A06 — acceptance-parity case.

Contract (`docs/spec/paridad/acceptance_cases.json`, A06): a lease that
expired and was taken over by a second worker must stop the FIRST worker
from producing an effect or writing a result once it wakes up — a plain
owner-string lease says "not mine anymore" just as well right up until the
same worker reclaims its own old row (a restart with a stable identity, a
retried heartbeat), at which point owner alone can no longer tell the zombie
attempt apart from the new one. `lease_generation` is the fix: a monotonic
counter bumped on every successful claim, so a worker compares the
generation ITS claim was handed against whatever the row holds right before
an effect, not just who currently owns it.

Two lease tables carry the mechanism, so both are exercised for real, through
the actual modules under test:

* `src.task_scheduler` — `ScheduledTask.lease_generation`, `_claim_due_task`,
  `TaskScheduler.still_owner`/`_still_owner_or_fenced`, and the fencing
  checkpoints in `_execute_task_locked`/`_execute_action`.
* `src.workflows.store` — `NodeRunRow.lease_generation`, `start_node`,
  `claim_active(..., generation=...)`, and `_check_fenced` in
  `src.workflows.handlers` (exercised through `approval_handler`, since
  `human_approval` is the one node type `recover_expired_node_leases`
  actually reopens for a second worker to reclaim, rather than marking
  `unknown_effect` outright).

In both scenarios: worker A claims (generation 1) and is then descheduled
(simulated — nothing here actually blocks a thread); its lease expires;
worker B claims the SAME occurrence/node (generation 2) and executes for
real, producing exactly one effect; worker A "wakes up" and attempts to
proceed — its own fencing check must say no, its effect must not run a
second time, and its result write must be rejected.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from tests.test_scheduler_lease_claim import add_task, db, make_scheduler, row  # noqa: F401
from tests.test_workflow_handlers import FakeApprovals, store, wf  # noqa: F401


def _utcnow():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(tzinfo=None)


def expire_lease(dbm, task_id):
    session = dbm.SessionLocal()
    try:
        session.query(dbm.ScheduledTask).filter(
            dbm.ScheduledTask.id == task_id).update(
            {"lease_expires_at": _utcnow() - timedelta(seconds=1)})
        session.commit()
    finally:
        session.close()


# ── scheduler: ScheduledTask.lease_generation ──────────────────────────────

@pytest.mark.acceptance("A06")
def test_a_fenced_scheduler_worker_does_not_execute_or_write_a_result(db, monkeypatch):  # noqa: F811
    from src.builtin_actions import BUILTIN_ACTIONS
    import src.task_scheduler as ts_mod

    calls = []

    async def fake_ping(**kwargs):
        calls.append(kwargs.get("owner"))
        return "pong", True

    monkeypatch.setitem(BUILTIN_ACTIONS, "a06_ping", fake_ping)

    now = _utcnow()
    due = now - timedelta(minutes=1)
    add_task(db, "t_fence", due=due)
    session = db.SessionLocal()
    try:
        session.query(db.ScheduledTask).filter(
            db.ScheduledTask.id == "t_fence").update(
            {"task_type": "action", "action": "a06_ping",
             "trigger_type": "event", "output_target": "notification"})
        session.commit()
    finally:
        session.close()

    sch_a = make_scheduler()
    sch_b = make_scheduler()

    # Worker A claims first: generation 1.
    assert sch_a._claim_due_task("t_fence", due, now) is True
    assert sch_a._lease_generation["t_fence"] == 1
    assert row(db, "t_fence").lease_generation == 1

    # Worker A is now "blocked" (a slow model-slot wait, a foreground-quiet
    # gate, GC pause -- whatever kept it from reaching its effect before its
    # lease ran out). The lease expires and worker B claims the same row.
    expire_lease(db, "t_fence")
    assert sch_b._claim_due_task("t_fence", due, now) is True
    assert sch_b._lease_generation["t_fence"] == 2
    assert row(db, "t_fence").lease_generation == 2, (
        "the takeover must bump the generation, not just move lease_owner")

    # Worker B executes for real: one call, real effect.
    task_row = row(db, "t_fence")
    result, success = asyncio.run(sch_b._execute_action(task_row))
    assert success is True and result == "pong"
    assert calls == ["luis"], "worker B's effect did not run exactly once"

    # Worker A wakes up: its OWN generation check must already say no, before
    # ever reaching the action.
    assert sch_a.still_owner("t_fence", 1) is False
    assert sch_a._still_owner_or_fenced("t_fence") is False
    with pytest.raises(ts_mod.TaskFenced):
        asyncio.run(sch_a._execute_action(task_row))
    assert calls == ["luis"], "a fenced worker A must not produce a second effect"

    # And the full path agrees: dispatched through _execute_task_locked, A's
    # attempt is fenced at the very first checkpoint (before task_type
    # dispatch), never reaching _execute_action at all.
    run_id_a = "run_a"
    tr = db.TaskRun(id=run_id_a, task_id="t_fence", started_at=_utcnow(),
                    status="queued", result="queued")
    session = db.SessionLocal()
    try:
        session.add(tr)
        session.commit()
    finally:
        session.close()

    before = row(db, "t_fence")
    asyncio.run(sch_a._execute_task_locked(
        "t_fence", run_id_a, release_executing=False, gate_foreground=False))

    after_run = db.SessionLocal().query(db.TaskRun).filter(
        db.TaskRun.id == run_id_a).first()
    assert after_run.status == "fenced", after_run.status
    assert "another worker took over" in (after_run.error or "")
    assert calls == ["luis"], "the fenced full-path attempt must not execute the action"

    after_task = row(db, "t_fence")
    assert after_task.next_run == before.next_run, (
        "a fenced attempt must not touch next_run -- that bookkeeping "
        "belongs to whichever worker still owns the occurrence")
    assert after_task.last_run == before.last_run


@pytest.mark.acceptance("A06")
def test_still_owner_is_false_once_a_takeover_bumps_the_generation(db):  # noqa: F811
    """`still_owner` in isolation, against the real column, independent of
    the scheduler's own `_lease_generation` bookkeeping."""
    sch = make_scheduler()
    now = _utcnow()
    due = now - timedelta(minutes=1)
    add_task(db, "t_owner", due=due)

    assert sch._claim_due_task("t_owner", due, now) is True
    assert sch.still_owner("t_owner", 1) is True

    expire_lease(db, "t_owner")
    assert sch._claim_due_task("t_owner", due, now) is True   # same worker, retakes it
    assert sch.still_owner("t_owner", 1) is False, (
        "even the SAME worker retaking its own row after an expiry is a new "
        "generation -- a heartbeat must never look like this, a takeover must")
    assert sch.still_owner("t_owner", 2) is True


# ── workflow: NodeRunRow.lease_generation ──────────────────────────────────

@pytest.mark.acceptance("A06")
def test_a_fenced_workflow_worker_does_not_open_a_second_card_or_write_a_result(store):  # noqa: F811
    from src.workflows.handlers import approval_handler

    d = wf({"id": "gate", "type": "human_approval",
           "config": {"action": "deliver", "detail": "send the report"}})
    run_id = store.create_run(d, owner="luis")["run_id"]
    store.set_run_status(run_id, "running")
    node = d.node("gate")

    # Worker A claims: generation 1.
    claim_a = store.start_node(run_id, node, attempt=1, worker_id="workerA")
    assert claim_a["claimed"] is True and claim_a["lease_generation"] == 1

    # A is now "blocked" before it ever calls the approvals store. Its lease
    # expires; recovery reopens the row (human_approval is not in
    # EFFECTFUL_TYPES, so a stalled gate is released, not marked
    # unknown_effect); worker B reclaims it.
    from core.database import NodeRunRow, SessionLocal
    with SessionLocal() as db_session:
        db_session.query(NodeRunRow).filter(
            NodeRunRow.workflow_run_id == run_id, NodeRunRow.node_id == "gate",
        ).update({"lease_expires_at": "2000-01-01T00:00:00Z"})
        db_session.commit()

    recovered = store.recover_expired_node_leases()
    assert [r["outcome"] for r in recovered] == ["released"]

    claim_b = store.start_node(run_id, node, attempt=2, worker_id="workerB")
    assert claim_b["claimed"] is True and claim_b["reason"] == "reclaimed"
    assert claim_b["lease_generation"] == 2, (
        "a reclaimed row must bump the generation, exactly like a fresh one")

    approvals = FakeApprovals()
    handler = approval_handler(approvals, owner="luis")

    # Worker B's own effect: the context is built exactly like
    # engine.py._run_node builds it, cancel_requested closed over B's own
    # worker_id/attempt/generation.
    context_b = {
        "run_id": run_id, "owner": "luis", "previous": {},
        "cancel_requested": lambda: not store.claim_active(
            run_id, "gate", worker_id="workerB", attempt=2,
            generation=claim_b["lease_generation"]),
    }
    out_b = handler(node, context_b)
    assert out_b["status"] == "paused" and out_b["approval_id"] == "apr_1"
    assert approvals.requests == 1, "worker B must open exactly one card"

    # The generation guard specifically (not merely attempt/worker_id, which
    # predate A06): even reusing B's own current worker_id/attempt with the
    # WRONG generation must be refused, while the right one still passes --
    # checked here, before finish_node ends the attempt and takes the row
    # out of "running" for everybody.
    assert store.claim_active(run_id, "gate", worker_id="workerB", attempt=2,
                              generation=1) is False
    assert store.claim_active(run_id, "gate", worker_id="workerB", attempt=2,
                              generation=2) is True

    assert store.finish_node(run_id, "gate", worker_id="workerB",
                             status="paused", result=out_b) is True

    # Worker A wakes up and tries the SAME effect with its OWN, stale
    # closure -- generation 1, attempt 1, its own worker_id. Real
    # `claim_active` must refuse it, `_check_fenced` must turn that into a
    # fenced result, and the handler must never call `approvals.request`
    # again.
    context_a = {
        "run_id": run_id, "owner": "luis", "previous": {},
        "cancel_requested": lambda: not store.claim_active(
            run_id, "gate", worker_id="workerA", attempt=1,
            generation=claim_a["lease_generation"]),
    }
    assert context_a["cancel_requested"]() is True, (
        "a stale worker's own cancel_requested must say True (cancelled) "
        "once a takeover has happened")
    out_a = handler(node, context_a)
    assert out_a == {
        "status": "failed", "fenced": True,
        "reason": "fenced: this node's lease was taken over by another worker "
                  "before its effect ran; not executed",
    }
    assert approvals.requests == 1, (
        "a fenced worker A must not open a second card for the same gate")

    # And its result write is rejected outright -- finish_node's own
    # worker_id-scoped WHERE clause, exercised for real.
    assert store.finish_node(run_id, "gate", worker_id="workerA",
                             status="completed", result={"approved": True}) is False
