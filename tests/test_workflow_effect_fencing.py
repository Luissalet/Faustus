"""A stale worker cannot mark or retry another worker's external effect."""
from core import database as db_mod
from core.database import NodeRunRow
from src.workflows import WorkflowEngine
from tests.test_workflow_handlers import store, wf  # noqa: F401


def active(store):
    definition = wf({"id": "send", "type": "deliver", "config": {}})
    run_id = store.create_run(definition, owner="alice")["run_id"]
    store.set_run_status(run_id, "running")
    store.start_node(run_id, definition.node("send"), attempt=1, worker_id="worker-a")
    return run_id


def test_only_the_current_worker_can_record_an_effect(store):
    run_id = active(store)
    assert not store.mark_effect(run_id, "send", "confirmed", worker_id="worker-b", attempt=1)
    assert store.mark_effect(run_id, "send", "pending", worker_id="worker-a", attempt=1)
    assert store.mark_effect(run_id, "send", "confirmed", worker_id="worker-a", attempt=1)
    assert not store.mark_effect(run_id, "send", "unknown", worker_id="worker-a", attempt=1)


def test_cancellation_prevents_start_but_can_record_an_inflight_confirmation(store):
    run_id = active(store)
    store.set_run_status(run_id, "cancelled")
    assert not store.mark_effect(run_id, "send", "pending", worker_id="worker-a", attempt=1)
    assert store.mark_effect(run_id, "send", "confirmed", worker_id="worker-a", attempt=1)
    assert store.get_run(run_id)["run"].status == "cancelled"


def test_a_late_release_cannot_clear_a_running_claim(store):
    run_id = active(store)
    assert not store.release_key(run_id, "send", 1)
    with db_mod.SessionLocal() as db:
        row = db.query(NodeRunRow).filter_by(workflow_run_id=run_id).one()
        assert row.lease_owner == "worker-a"
        assert row.idempotency_key


def test_cancellation_probe_is_bound_to_live_worker_attempt_and_lease(store):
    run_id = active(store)
    assert store.claim_active(run_id, "send", worker_id="worker-a", attempt=1)
    assert not store.claim_active(run_id, "send", worker_id="worker-b", attempt=1)
    assert not store.claim_active(run_id, "send", worker_id="worker-a", attempt=2)
    with db_mod.SessionLocal() as db:
        row = db.query(NodeRunRow).filter_by(workflow_run_id=run_id).one()
        row.lease_expires_at = "2000-01-01T00:00:00Z"
        db.commit()
    assert not store.claim_active(run_id, "send", worker_id="worker-a", attempt=1)


def test_engine_exposes_live_cancellation_to_its_handler(store):
    definition = wf({"id": "send", "type": "deliver", "config": {}})

    def handler(node, context):
        assert not context["cancel_requested"]()
        store.set_run_status(context["run_id"], "cancelled")
        assert context["cancel_requested"]()
        return {"status": "completed"}

    run_id = store.create_run(definition, owner="alice")["run_id"]
    WorkflowEngine({"deliver": handler}, store).advance(run_id)
    assert store.get_run(run_id)["run"].status == "cancelled"


def test_unknown_effect_is_not_retried_even_if_config_claims_idempotence(store):
    definition = wf({"id": "send", "type": "deliver", "max_attempts": 3,
                     "config": {"idempotent": True}})
    calls = []

    def send(node, context):
        calls.append(context["attempt"])
        assert context["idempotency_key"]
        assert context["mark_effect"]("pending")
        assert context["mark_effect"]("unknown")
        raise ConnectionError("delivery receipt lost")

    run_id = store.create_run(definition, owner="alice")["run_id"]
    outcome = WorkflowEngine({"deliver": send}, store).advance(run_id)
    assert outcome["status"] == "failed"
    assert calls == [1]
    assert len(store.needs_reconciliation(run_id=run_id)) == 1
