"""The execution boundary must spend permissions, not just look at them."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from src import approval_store, execution_router as router
from tests.test_approval_gate_on_runs import stage, manifest, _run  # noqa: F401


def grant_for(stage, man, *, owner="luis", uses=1):
    decision = router.choose(man, workspace=stage["workspace"],
                             artifacts_root=stage["artifacts_root"], run_id="preview",
                             create_dirs=False)
    cards = []
    for action in man.effective_approvals():
        card = approval_store.request(router.plan_for(man, decision.spec, action, command=["echo", "hi"]),
                                      owner=owner, uses=uses)
        approval_store.decide(card.id, granted=True, by=owner)
        cards.append(card)
    return cards


def test_one_permission_starts_only_one_execution(stage):
    card, = grant_for(stage, manifest())
    assert _run(stage, manifest())[1].status == "completed"
    assert _run(stage, manifest(), run_id="second")[1].status == "refused"
    assert len(stage["started"]) == 1
    assert approval_store.get(card.id).status == "consumed"


def test_two_concurrent_executions_cannot_share_permission(stage, monkeypatch):
    grant_for(stage, manifest())
    gate = threading.Barrier(2)
    original_build = router.execution_backends.build

    def simultaneous_build(*args, **kwargs):
        gate.wait(timeout=5)
        return original_build(*args, **kwargs)

    monkeypatch.setattr(router.execution_backends, "build", simultaneous_build)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_run, stage, manifest(), run_id=f"concurrent-{i}")
                   for i in range(2)]
        results = [f.result(timeout=10)[1] for f in futures]
    assert sorted(r.status for r in results) == ["completed", "refused"]
    assert len(stage["started"]) == 1


def test_missing_secret_does_not_spend_permissions(stage):
    man = manifest(permissions={"backends": ["docker_workspace"], "max_seconds": 60,
                                "secrets": ["smtp"]})
    cards = grant_for(stage, man)
    assert _run(stage, man)[1].status == "refused"
    assert not stage["started"]
    assert all(approval_store.get(c.id).uses_left == 1 for c in cards)


def test_backend_exception_does_not_refund_a_possibly_executed_action(stage, monkeypatch):
    card, = grant_for(stage, manifest())

    class FailedBackend:
        def run(self, *args, **kwargs):
            raise RuntimeError("connection lost after sending command")

    monkeypatch.setattr(router.execution_backends, "build", lambda *a, **kw: FailedBackend())
    with pytest.raises(RuntimeError):
        _run(stage, manifest())
    assert approval_store.get(card.id).status == "consumed"


def test_batch_does_not_spend_earlier_permissions_if_one_is_missing(stage):
    card, = grant_for(stage, manifest())
    missing = {**card.plan.to_dict(), "action": "deliver"}
    result = approval_store.consume_plans([card.plan, missing], owner="luis")
    assert not result["ok"]
    assert approval_store.get(card.id).uses_left == 1
    assert approval_store.check(card.plan, owner="luis")["ok"]


def test_batch_cannot_spend_another_owners_permission(stage):
    card, = grant_for(stage, manifest(), owner="alice")
    assert not approval_store.consume_plans([card.plan], owner="luis")["ok"]
    assert approval_store.get(card.id).uses_left == 1


def test_batch_spends_each_required_permission_once(stage):
    man = manifest(approval={"required_when": ["publish", "deliver"]})
    cards = grant_for(stage, man, uses=2)
    assert _run(stage, man)[1].status == "completed"
    assert all(approval_store.get(c.id).uses_left == 1 for c in cards)
    assert _run(stage, man, run_id="second")[1].status == "completed"
    assert all(approval_store.get(c.id).status == "consumed" for c in cards)
    assert _run(stage, man, run_id="third")[1].status == "refused"
