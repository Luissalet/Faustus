"""A stored 'granted' label is not enough to authorize a waiting workflow."""
from core import database as db_mod
from core.database import ApprovalRow
from src import approval_store
from src.workflows import WorkflowEngine, default_handlers
from src.workflows.scheduler import WorkflowScheduler
from tests.test_workflow_handlers import store, wf  # noqa: F401


def start(store):
    definition = wf({"id": "gate", "type": "human_approval", "config": {"action": "deliver"}},
                    {"id": "done", "type": "manual", "needs": ["gate"]})
    run = store.create_run(definition, owner="alice")["run_id"]
    engine = WorkflowEngine(default_handlers(), store)
    first = engine.advance(run)
    return run, engine, first["approval_id"]


def test_expired_grant_is_rejected_even_before_a_cleanup_sweep(store):
    run, _, card_id = start(store)
    approval_store.decide(card_id, granted=True, by="alice")
    with db_mod.SessionLocal() as db:
        db.query(ApprovalRow).filter_by(id=card_id).update({"expires_at": "2000-01-01T00:00:00Z"})
        db.commit()
    assert WorkflowScheduler(store).tick()[0]["status"] == "failed"
    assert "expired" in store.node_runs(run)["gate"].reason
    assert "done" not in store.node_runs(run)


def test_a_pending_expired_card_fails_without_waiting_for_a_person(store):
    run, _, card_id = start(store)
    with db_mod.SessionLocal() as db:
        db.query(ApprovalRow).filter_by(id=card_id).update({"expires_at": "2000-01-01T00:00:00Z"})
        db.commit()
    assert WorkflowScheduler(store).tick()[0]["status"] == "failed"
    assert "done" not in store.node_runs(run)


def test_a_foreign_approval_cannot_unblock_an_identical_plan(store):
    run, engine, card_id = start(store)
    original = approval_store.get(card_id)
    other = approval_store.request(original.plan, owner="bob")
    approval_store.decide(other.id, granted=True, by="bob")
    store.finish_node(run, "gate", status="paused", result={"approval_id": other.id})
    assert engine.resume(run, "gate")["status"] == "failed"
    assert "another owner" in store.node_runs(run)["gate"].reason


def test_a_changed_plan_cannot_unblock_the_old_workflow(store):
    run, engine, card_id = start(store)
    original = approval_store.get(card_id)
    changed = approval_store.request({**original.plan.to_dict(), "detail": "something else"}, owner="alice")
    approval_store.decide(changed.id, granted=True, by="alice")
    store.finish_node(run, "gate", status="paused", result={"approval_id": changed.id})
    assert engine.resume(run, "gate")["status"] == "failed"
    assert "plan changed" in store.node_runs(run)["gate"].reason


def test_approved_gate_continues_automatically_but_denied_one_stops(store):
    run, _, card_id = start(store)
    approval_store.decide(card_id, granted=True, by="alice")
    assert WorkflowScheduler(store).tick()[0]["status"] == "completed"
    denied, _, card_id = start(store)
    approval_store.decide(card_id, granted=False, by="alice")
    assert WorkflowScheduler(store).tick()[0]["status"] == "failed"
    assert "done" not in store.node_runs(denied)
