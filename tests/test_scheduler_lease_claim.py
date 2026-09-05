"""tests/test_scheduler_lease_claim.py — B-014: two schedulers, one occurrence.

The scheduler used to decide "am I already running this?" with a set in one
interpreter. That is a real guard for two coroutines and no guard at all for a
second worker, a second server window, or a restart landing on a row whose
next_run is still in the past — and the task that gets picked up twice is the
one that sends the email twice.

So the test that matters is the one the audit asked for: two TaskScheduler
instances over the SAME SQLite file, released together by a threading barrier
placed immediately in front of the claim, counting dispatches rather than
statuses. Everything else here is the same claim from a different angle — a
lease that expires and is recovered, an occurrence key that stays the same
across that recovery, and a delivery target that is not allowed to be retried
at all.
"""
from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from core import database as db_mod
from core.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """A real SQLite FILE, not the shared in-memory database: the claim is a
    write from a second connection, and two connections is the whole point."""
    url = "sqlite:///" + (tmp_path / "sched.db").as_posix()
    engine = create_engine(url, connect_args={"check_same_thread": False},
                           poolclass=NullPool)
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal",
                        sessionmaker(autocommit=False, autoflush=False, bind=engine))
    Base.metadata.create_all(bind=engine)
    try:
        yield db_mod
    finally:
        engine.dispose()


def make_scheduler():
    """A TaskScheduler without its __init__: this file exercises the claim,
    not the session manager, the model slot or the notification queue."""
    from src.task_scheduler import TaskScheduler
    sch = TaskScheduler.__new__(TaskScheduler)
    sch._session_manager = None
    sch._running = True
    sch._task = None
    sch._executing = set()
    sch._executing_lock = asyncio.Lock()
    sch._pending_notifications = []
    sch._task_defer_counts = {}
    sch._run_semaphore = asyncio.Semaphore(1)
    sch._concurrency_cap = 1
    sch._task_handles = {}
    sch._lease_warned = False
    return sch


def add_task(dbm, task_id, *, due, target="session", schedule="daily",
             scheduled_time="09:00"):
    session = dbm.SessionLocal()
    try:
        session.add(dbm.ScheduledTask(
            id=task_id, owner="luis", name=task_id, task_type="llm",
            prompt="say something", schedule=schedule,
            scheduled_time=scheduled_time, trigger_type="schedule",
            next_run=due, status="active", output_target=target))
        session.commit()
    finally:
        session.close()


def row(dbm, task_id):
    session = dbm.SessionLocal()
    try:
        return session.query(dbm.ScheduledTask).filter(
            dbm.ScheduledTask.id == task_id).first()
    finally:
        session.close()


# ── the claim ─────────────────────────────────────────────────────────────

def test_two_schedulers_on_one_database_dispatch_one_due_task_once(db, monkeypatch):
    """The audit's test. Two schedulers, one SQLite file, both released into
    the due-scan by a threading barrier. Exactly one dispatch.

    The barrier sits in `has_foreground_activity`, the first thing
    `_check_due_tasks` calls, rather than around the claim itself: that is a
    seam both the old code and the new one go through, so this test counts
    what the schedulers actually did instead of asserting that a new method
    exists. Nothing after the barrier changes `status` or `next_run`, so the
    loser's own scan still finds the task due and the claim is the only thing
    that can stop it running a second time.
    """
    import src.interactive_gate as gate

    add_task(db, "t_race", due=_utcnow() - timedelta(minutes=5))

    barrier = threading.Barrier(2, timeout=20)
    dispatched = []
    guard = threading.Lock()

    def at_the_gate():
        barrier.wait()          # both schedulers enter the scan together
        return False

    monkeypatch.setattr(gate, "has_foreground_activity", at_the_gate)

    def run_one(name):
        async def drive():
            sch = make_scheduler()

            async def fake_execute(task_id, **kwargs):
                with guard:
                    dispatched.append((name, task_id))

            sch._execute_task = fake_execute
            await sch._check_due_tasks()
            await asyncio.sleep(0.05)   # let the dispatched task actually run

        asyncio.run(drive())

    threads = [threading.Thread(target=run_one, args=(n,), daemon=True)
               for n in ("worker-a", "worker-b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive(), "a scheduler thread never finished"

    assert len(dispatched) == 1, (
        f"both schedulers ran the same due task: {dispatched}")

    claimed = row(db, "t_race")
    assert claimed.lease_owner, "the winner left no lease behind"
    assert claimed.lease_attempt == 1
    assert claimed.lease_key == f"task:t_race:{claimed.next_run.replace(microsecond=0).isoformat()}"


def test_a_second_claim_on_a_leased_task_loses(db):
    sch = make_scheduler()
    now = _utcnow()
    due = now - timedelta(minutes=1)
    add_task(db, "t_one", due=due)

    assert sch._claim_due_task("t_one", due, now) is True
    assert sch._claim_due_task("t_one", due, now) is False, (
        "a live lease did not stop a second claim")


def test_a_task_that_is_not_due_yet_cannot_be_claimed(db):
    """The claim's WHERE clause carries the whole rule, not just the lease:
    a row nobody holds but nothing is owed on must still refuse."""
    sch = make_scheduler()
    now = _utcnow()
    future = now + timedelta(hours=2)
    add_task(db, "t_future", due=future)

    assert sch._claim_due_task("t_future", future, now) is False
    assert row(db, "t_future").lease_owner is None


def test_a_paused_task_cannot_be_claimed_even_when_overdue(db):
    sch = make_scheduler()
    now = _utcnow()
    due = now - timedelta(hours=1)
    add_task(db, "t_paused", due=due)
    session = db.SessionLocal()
    try:
        session.query(db.ScheduledTask).filter(
            db.ScheduledTask.id == "t_paused").update({"status": "paused"})
        session.commit()
    finally:
        session.close()

    assert sch._claim_due_task("t_paused", due, now) is False


# ── the lease, and what happens when its holder dies ──────────────────────

def expire_lease(dbm, task_id, *, owner="ghost-worker"):
    """Pretend the process holding this claim was killed."""
    session = dbm.SessionLocal()
    try:
        session.query(dbm.ScheduledTask).filter(
            dbm.ScheduledTask.id == task_id).update(
            {"lease_owner": owner,
             "lease_expires_at": _utcnow() - timedelta(seconds=1)})
        session.commit()
    finally:
        session.close()


def test_a_recovered_lease_keeps_the_occurrence_key_and_counts_the_attempt(db):
    """The point of a stable key. Attempt two of the same occurrence has to
    carry the string attempt one carried, or a destination that deduplicates
    has nothing to match against."""
    sch = make_scheduler()
    now = _utcnow()
    due = now - timedelta(minutes=1)
    add_task(db, "t_recover", due=due, target="session")

    assert sch._claim_due_task("t_recover", due, now) is True
    first_key = row(db, "t_recover").lease_key

    expire_lease(db, "t_recover")
    recovered = sch._recover_expired_leases()
    assert [r["action"] for r in recovered] == ["released"]
    assert recovered[0]["idempotency_key"] == first_key

    assert sch._claim_due_task("t_recover", due, _utcnow()) is True
    after = row(db, "t_recover")
    assert after.lease_key == first_key, "the retry invented a new key"
    assert after.lease_attempt == 2


def test_an_occurrence_that_must_not_repeat_is_abandoned_not_retried(db):
    """`at_most_once` is the whole reason the semantics exist. A worker died
    mid-send; re-dispatching is how the message goes out twice, so the
    occurrence is dropped, the drop is written into Activity, and the task
    moves on to its next scheduled moment."""
    sch = make_scheduler()
    now = _utcnow()
    due = now - timedelta(minutes=1)
    add_task(db, "t_mail", due=due, target="email")

    assert sch._claim_due_task("t_mail", due, now) is True
    expire_lease(db, "t_mail")

    recovered = sch._recover_expired_leases()
    assert [r["action"] for r in recovered] == ["abandoned"]
    assert recovered[0]["semantics"] == "at_most_once"

    after = row(db, "t_mail")
    assert after.lease_owner is None
    assert after.lease_key is None, "the abandoned occurrence stayed claimable"
    assert after.next_run > now, "the abandoned occurrence is still due"

    session = db.SessionLocal()
    try:
        runs = session.query(db.TaskRun).filter(db.TaskRun.task_id == "t_mail").all()
        assert [r.status for r in runs] == ["aborted"]
        assert "abandoned rather than repeated" in (runs[0].error or "")
    finally:
        session.close()


def test_a_live_heartbeat_keeps_the_claim_out_of_the_recovery_sweep(db):
    """A task that legitimately runs for an hour must not be handed to a
    second worker at minute sixteen."""
    sch = make_scheduler()
    now = _utcnow()
    due = now - timedelta(minutes=1)
    add_task(db, "t_slow", due=due)

    assert sch._claim_due_task("t_slow", due, now) is True
    expire_lease(db, "t_slow", owner=None)      # cleared below by the beat
    session = db.SessionLocal()
    try:
        from src.task_scheduler import WORKER_ID
        session.query(db.ScheduledTask).filter(
            db.ScheduledTask.id == "t_slow").update({"lease_owner": WORKER_ID})
        session.commit()
    finally:
        session.close()

    assert sch._heartbeat_lease("t_slow") is True
    assert sch._recover_expired_leases() == [], (
        "a heartbeat did not stop the sweep from reclaiming a live run")


def test_releasing_a_lease_ends_the_occurrence(db):
    """A clean release drops the key too: the next time this task is due it is
    a NEW occurrence at attempt one, not attempt three of the last one."""
    sch = make_scheduler()
    now = _utcnow()
    due = now - timedelta(minutes=1)
    add_task(db, "t_done", due=due)

    assert sch._claim_due_task("t_done", due, now) is True
    assert sch._release_lease("t_done") is True

    after = row(db, "t_done")
    assert after.lease_owner is None and after.lease_key is None
    assert after.lease_attempt == 0

    later = due + timedelta(days=1)
    session = db.SessionLocal()
    try:
        session.query(db.ScheduledTask).filter(
            db.ScheduledTask.id == "t_done").update({"next_run": later})
        session.commit()
    finally:
        session.close()

    assert sch._claim_due_task("t_done", later, later + timedelta(seconds=1)) is True
    assert row(db, "t_done").lease_key.endswith(later.replace(microsecond=0).isoformat())


# ── what may honestly be promised ─────────────────────────────────────────

def test_the_occurrence_key_names_the_moment_not_the_attempt():
    from src.task_scheduler import occurrence_key
    due = datetime(2026, 3, 1, 9, 0, 0)
    assert occurrence_key("t1", due) == occurrence_key("t1", due)
    assert occurrence_key("t1", due) != occurrence_key("t1", due + timedelta(days=1))
    assert occurrence_key("t1", due) != occurrence_key("t2", due)
    assert occurrence_key("t1", None).endswith("unscheduled")


@pytest.mark.parametrize("target,expected", [
    ("session", "at_least_once"),
    ("notification", "at_least_once"),
    (None, "at_least_once"),
    ("email", "at_most_once"),
    ("webhook", "at_most_once"),
])
def test_delivery_semantics_are_read_off_the_destination(target, expected):
    from src.task_scheduler import delivery_semantics

    class _Task:
        output_target = target

    assert delivery_semantics(_Task()) == expected


def test_nothing_is_promised_effectively_once_while_no_sink_deduplicates():
    """The honesty check. `effectively_once` is only true when the far end
    refuses a key it has seen; no Faustus delivery path does that yet, so the
    scheduler must never return it."""
    from src.task_scheduler import (
        EFFECTIVELY_ONCE, IDEMPOTENT_SINKS, delivery_semantics)

    assert IDEMPOTENT_SINKS == frozenset()

    class _Task:
        output_target = "email"

    assert delivery_semantics(_Task()) != EFFECTIVELY_ONCE


def test_the_current_occurrence_is_what_an_adapter_would_deduplicate_on(db):
    sch = make_scheduler()
    now = _utcnow()
    due = now - timedelta(minutes=1)
    add_task(db, "t_seam", due=due, target="email")
    sch._claim_due_task("t_seam", due, now)

    seam = sch.current_occurrence("t_seam")
    assert seam["idempotency_key"] == row(db, "t_seam").lease_key
    assert seam["attempt"] == 1
    assert seam["semantics"] == "at_most_once"
    assert seam["lease_owner"]
