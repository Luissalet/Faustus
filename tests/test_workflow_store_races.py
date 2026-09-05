"""tests/test_workflow_store_races.py — B-015: the losing side of a race.

`create_run` and `start_node` both used to read, then write. Two callers can
both pass the read, and the one that loses the unique index got an
`IntegrityError` thrown at it — a crash reported for work another process was
doing perfectly well. And a worker killed while holding a node's claim left
`running` behind forever, with the key on it that refuses every retry: a node
stuck by design.

So this file races the two claims through the store's own fault points (a
barrier installed where the process could die is the same seam as a barrier
installed where two processes could collide), and then asks the harder
question the audit put next to it: after the crash, what does the row mean?
The answer differs by node type, which is why `unknown_effect` has to be kept
apart from `failed`.
"""
from __future__ import annotations

import threading

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from core import database as db_mod
from core.database import Base
from src.contracts import WorkflowDefinition
from src.contracts.base import now_iso
from src.workflows import WorkflowEngine, WorkflowStore
from src.workflows import store as store_mod


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """A real SQLite FILE and no connection pool: the races below run in two
    threads, and a shared connection would serialise the very thing under
    test."""
    url = "sqlite:///" + (tmp_path / "wf.db").as_posix()
    engine = create_engine(url, connect_args={"check_same_thread": False},
                           poolclass=NullPool)
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal",
                        sessionmaker(autocommit=False, autoflush=False, bind=engine))
    Base.metadata.create_all(bind=engine)
    previous = store_mod.set_fault_hook(None)
    try:
        yield WorkflowStore()
    finally:
        store_mod.set_fault_hook(previous)
        engine.dispose()


def definition(**over):
    body = {
        "id": "report.publish", "version": "1.0.0", "title": "Write and send",
        "nodes": [
            {"id": "gather", "type": "skill", "config": {"skill": "research"}},
            {"id": "send", "type": "deliver", "needs": ["gather"],
             "config": {"to": "ana@example.com"}},
        ],
    }
    body.update(over)
    return WorkflowDefinition.parse(body)


def node_of(defn, node_id):
    return defn.node(node_id)


def rows(dbm, run_id):
    session = dbm.SessionLocal()
    try:
        return session.query(dbm.NodeRunRow).filter(
            dbm.NodeRunRow.workflow_run_id == run_id).all()
    finally:
        session.close()


def race(barrier_point, work, *, workers=2):
    """Run `work(index)` in two threads, released together at `barrier_point`.

    The barrier goes in the store's own fault hook rather than around the
    call, so both threads are inside the claim — past the lookup, at the
    insert — when they are let go. That is the window the bug lives in.
    """
    barrier = threading.Barrier(workers, timeout=20)
    results, errors = [None] * workers, [None] * workers

    def hook(point, **_):
        if point == barrier_point:
            barrier.wait()

    def run(index):
        try:
            results[index] = work(index)
        except BaseException as e:            # noqa: BLE001 — reported, not swallowed
            errors[index] = e

    store_mod.set_fault_hook(hook)
    try:
        threads = [threading.Thread(target=run, args=(i,), daemon=True)
                   for i in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
            assert not t.is_alive(), "a racing thread never finished"
    finally:
        store_mod.set_fault_hook(None)
    return results, errors


# ── two callers, one claim ────────────────────────────────────────────────

def test_two_simultaneous_triggers_both_get_the_winning_run(store):
    """A redelivered webhook arriving twice at once. The loser must come back
    with the run that exists, not with the database's complaint about it."""
    defn = definition()
    results, errors = race(
        "before_run_insert",
        lambda _i: store.create_run(defn, dedupe_key="webhook-evt-42"))

    assert errors == [None, None], f"a caller was handed the race to lose: {errors}"
    assert sorted(r["created"] for r in results) == [False, True]
    assert results[0]["run_id"] == results[1]["run_id"]
    assert {r["reason"] for r in results} == {"created", "duplicate_trigger"}

    session = db_mod.SessionLocal()
    try:
        assert session.query(db_mod.WorkflowRunRow).count() == 1
    finally:
        session.close()


def test_two_passes_claiming_one_node_open_one_attempt(store):
    """The same race a node down. The loser is told the attempt is already
    open — and, crucially, is told it in the shape a normal loss uses, so the
    engine's `already_attempted` branch handles it without knowing there was
    a race at all."""
    defn = definition()
    run_id = store.create_run(defn)["run_id"]
    node = node_of(defn, "gather")

    results, errors = race(
        "before_insert",
        lambda _i: store.start_node(run_id, node, attempt=1, inputs={}))

    assert errors == [None, None], f"a claim raised instead of losing: {errors}"
    assert sorted(r["claimed"] for r in results) == [False, True]
    loser = next(r for r in results if not r["claimed"])
    assert loser["reason"] == "already_attempted"
    assert loser["status"] == "running"
    assert loser["idempotency_key"] == next(
        r for r in results if r["claimed"])["idempotency_key"]

    assert len(rows(db_mod, run_id)) == 1, "the race opened a second attempt"


def test_a_claim_carries_a_lease_rather_than_a_permanent_running(store):
    defn = definition()
    run_id = store.create_run(defn)["run_id"]
    claim = store.start_node(run_id, node_of(defn, "gather"), attempt=1)

    assert claim["claimed"] is True
    assert claim["lease_expires_at"] > now_iso(), "the claim was born expired"
    row = rows(db_mod, run_id)[0]
    assert row.status == "running"
    assert row.lease_owner == claim["worker_id"]
    assert row.lease_expires_at == claim["lease_expires_at"]
    assert row.effect_state == "none"


def test_finishing_a_node_hands_the_lease_back(store):
    defn = definition()
    run_id = store.create_run(defn)["run_id"]
    store.start_node(run_id, node_of(defn, "gather"), attempt=1)
    assert store.finish_node(run_id, "gather", status="completed",
                             result={"ok": True}) is True

    row = rows(db_mod, run_id)[0]
    assert row.lease_owner is None and row.lease_expires_at is None
    assert row.effect_state == "confirmed", (
        "the handler returned, which is the only evidence the store gets")


# ── a claim whose holder stopped answering ────────────────────────────────

def expire_lease(dbm, run_id, node_id):
    """Age the lease past its end without waiting a quarter of an hour."""
    session = dbm.SessionLocal()
    try:
        session.query(dbm.NodeRunRow).filter(
            dbm.NodeRunRow.workflow_run_id == run_id,
            dbm.NodeRunRow.node_id == node_id).update(
            {"lease_expires_at": "2000-01-01T00:00:00Z"})
        session.commit()
    finally:
        session.close()


def test_a_worker_that_died_across_an_effect_leaves_unknown_not_failed(store):
    """The distinction the audit asked for. The delivery node acted and then
    the process died before its result was written; nobody saw whether the
    message went. Retrying might send it twice and writing it off as a plain
    failure hides that somebody has to go and look."""
    defn = definition(nodes=[{"id": "send", "type": "deliver",
                              "config": {"to": "ana@example.com"}}])
    run_id = store.create_run(defn)["run_id"]
    sent = []

    def deliver(node, ctx):
        sent.append(node.config["to"])
        raise SystemExit("power cut")      # after the effect, before the result

    with pytest.raises(SystemExit):
        WorkflowEngine({"deliver": deliver}, store).advance(run_id)
    assert sent == ["ana@example.com"]

    expire_lease(db_mod, run_id, "send")
    handled = store.recover_expired_node_leases()
    assert [h["outcome"] for h in handled] == ["unknown_effect"]
    assert handled[0]["node_type"] == "deliver"

    waiting = store.needs_reconciliation(run_id=run_id)
    assert len(waiting) == 1 and waiting[0]["node_id"] == "send"
    assert "reconcil" in waiting[0]["reason"]
    assert waiting[0]["idempotency_key"], (
        "the key is what a reconciliation would ask the provider about")

    # And nothing quietly sends it a second time on the way past.
    again = WorkflowEngine({"deliver": lambda n, c: sent.append("again") or {}},
                           WorkflowStore()).advance(run_id)
    assert sent == ["ana@example.com"], "recovery re-ran a node with an unknown effect"
    assert again["status"] == "failed"


def test_a_worker_that_died_on_a_node_reaching_nothing_is_simply_released(store):
    """The other half of the same decision. A condition touches nothing
    outside Faustus, so an interrupted attempt at it is just an interrupted
    attempt: release the key and let the next pass have another go."""
    defn = definition(nodes=[{"id": "check", "type": "condition",
                              "config": {"left": "a", "op": "eq", "right": "a"}}])
    run_id = store.create_run(defn)["run_id"]
    store.start_node(run_id, node_of(defn, "check"), attempt=1)

    expire_lease(db_mod, run_id, "check")
    handled = store.recover_expired_node_leases()
    assert [h["outcome"] for h in handled] == ["released"]

    row = rows(db_mod, run_id)[0]
    assert row.status == "pending" and row.idempotency_key is None
    assert row.lease_owner is None

    retry = store.start_node(run_id, node_of(defn, "check"), attempt=2)
    assert retry["claimed"] is True and retry["reason"] == "reclaimed"
    assert store.needs_reconciliation() == []


def test_a_handler_that_confirmed_its_effect_needs_no_reconciliation(store):
    """`mark_effect` is what a handler has instead of hope. Having said the
    provider answered, the only thing lost to the crash is the result — so
    this is neither retried nor put in front of a person."""
    defn = definition(nodes=[{"id": "send", "type": "deliver",
                              "config": {"to": "ana@example.com"}}])
    run_id = store.create_run(defn)["run_id"]
    store.start_node(run_id, node_of(defn, "send"), attempt=1)
    assert store.mark_effect(run_id, "send", "pending") is True
    assert store.mark_effect(run_id, "send", "confirmed") is True

    expire_lease(db_mod, run_id, "send")
    handled = store.recover_expired_node_leases()
    assert [h["outcome"] for h in handled] == ["effect_confirmed"]
    assert store.needs_reconciliation(run_id=run_id) == []


def test_a_heartbeat_keeps_a_long_node_out_of_the_recovery_sweep(store):
    defn = definition()
    run_id = store.create_run(defn)["run_id"]
    claim = store.start_node(run_id, node_of(defn, "gather"), attempt=1)

    expire_lease(db_mod, run_id, "gather")
    assert store.heartbeat_node(run_id, "gather", worker_id=claim["worker_id"]) is True
    assert store.recover_expired_node_leases() == [], (
        "a node whose worker is plainly alive was taken off it")


def test_a_heartbeat_from_somebody_else_does_not_move_our_lease(store):
    defn = definition()
    run_id = store.create_run(defn)["run_id"]
    store.start_node(run_id, node_of(defn, "gather"), attempt=1)
    assert store.heartbeat_node(run_id, "gather", worker_id="some-other-box:1:ffff") is False


# ── killing it at each boundary ───────────────────────────────────────────

def kill_once(point):
    """A fault hook that kills the process the first time it passes `point`.

    Once only: the second pass is the one being tested, and a hook that fired
    again would be testing the hook.
    """
    fired = []

    def hook(seen, **_):
        if seen == point and not fired:
            fired.append(seen)
            raise SystemExit(f"power cut at {seen}")

    return hook


@pytest.mark.parametrize("point", ["before_claim", "before_insert"])
def test_a_crash_before_the_claim_leaves_the_work_still_to_do(store, point):
    """Dying before the row exists must not cost the node its turn: nothing
    was claimed, nothing was done, and the next pass does the work once."""
    defn = definition(nodes=[{"id": "send", "type": "deliver",
                              "config": {"to": "ana@example.com"}}])
    run_id = store.create_run(defn)["run_id"]
    sent = []
    handlers = {"deliver": lambda n, c: sent.append(n.config["to"]) or {"ok": True}}

    store_mod.set_fault_hook(kill_once(point))
    try:
        with pytest.raises(SystemExit):
            WorkflowEngine(handlers, store).advance(run_id)
    finally:
        store_mod.set_fault_hook(None)
    assert sent == [] and rows(db_mod, run_id) == []

    result = WorkflowEngine(handlers, WorkflowStore()).advance(run_id)
    assert result["status"] == "completed"
    assert sent == ["ana@example.com"]


def test_a_crash_after_the_claim_refuses_to_run_the_node_again(store):
    """The claim is written before the work for exactly this reason. Dying
    between the two is the case where the store genuinely cannot know whether
    the effect happened, so the honest outcome is a refusal plus a row that
    asks for reconciliation — not a second delivery."""
    defn = definition(nodes=[{"id": "send", "type": "deliver",
                              "config": {"to": "ana@example.com"}}])
    run_id = store.create_run(defn)["run_id"]
    sent = []
    handlers = {"deliver": lambda n, c: sent.append(n.config["to"]) or {"ok": True}}

    store_mod.set_fault_hook(kill_once("after_claim"))
    try:
        with pytest.raises(SystemExit):
            WorkflowEngine(handlers, store).advance(run_id)
    finally:
        store_mod.set_fault_hook(None)
    assert sent == []

    result = WorkflowEngine(handlers, WorkflowStore()).advance(run_id)
    assert sent == [], "a claimed attempt was run a second time"
    assert result["ran"][0]["reason"] == "already_attempted"

    expire_lease(db_mod, run_id, "send")
    assert [h["outcome"] for h in store.recover_expired_node_leases()] == ["unknown_effect"]


def test_every_boundary_the_audit_named_has_a_fault_point():
    """Before the claim, after the commit, either side of the effect, and
    before the result is saved. A boundary with no name is a boundary no test
    can crash at."""
    for point in ("before_claim", "after_claim", "before_effect",
                  "after_effect", "before_result"):
        assert point in store_mod.FAULT_POINTS


def test_the_fault_hook_is_off_unless_something_installs_one(store):
    """It has to cost nothing in production, and it has to be restorable: a
    test that left the hook in place would kill whatever ran next."""
    assert store_mod._fault_hook is None
    previous = store_mod.set_fault_hook(lambda point, **kw: None)
    assert previous is None
    assert store_mod.set_fault_hook(previous) is not None
    assert store_mod._fault_hook is None
