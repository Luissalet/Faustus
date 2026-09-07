"""Changing the command, directory or image changes what was approved."""
from src import approval_store, execution_router as router
from tests.test_approval_gate_on_runs import stage, manifest, _run  # noqa: F401


def test_another_command_cannot_borrow_the_same_skill_approval(stage):
    _run(stage, manifest())
    card = approval_store.pending(owner="luis")[0]
    approval_store.decide(card.id, granted=True, by="luis")
    _, result = router.execute(manifest(), ["echo", "different action"],
                               workspace=stage["workspace"], artifacts_root=stage["artifacts_root"],
                               run_id="different", owner="luis")
    assert result.status == "refused"
    assert not stage["started"]
    assert approval_store.get(card.id).uses_left == 1


def test_another_workspace_cannot_borrow_the_same_skill_approval(stage, tmp_path):
    _run(stage, manifest())
    card = approval_store.pending(owner="luis")[0]
    approval_store.decide(card.id, granted=True, by="luis")
    other = tmp_path / "other-project"
    other.mkdir()
    _, result = router.execute(manifest(), ["echo", "hi"], workspace=str(other),
                               artifacts_root=stage["artifacts_root"], run_id="different", owner="luis")
    assert result.status == "refused"
    assert not stage["started"]


def test_another_container_image_cannot_borrow_the_same_skill_approval(stage):
    _run(stage, manifest())
    card = approval_store.pending(owner="luis")[0]
    approval_store.decide(card.id, granted=True, by="luis")
    assert _run(stage, manifest(), image="another-image:1")[1].status == "refused"
    assert not stage["started"]


def test_command_lists_are_copied_before_the_execution_boundary(stage, monkeypatch):
    command = ["echo", "original"]
    decision = router.choose(manifest(), workspace=stage["workspace"],
                             artifacts_root=stage["artifacts_root"], run_id="preview", create_dirs=False)
    plan = router.plan_for(manifest(), decision.spec, "publish", command=command)
    card = approval_store.request(plan, owner="luis")
    approval_store.decide(card.id, granted=True, by="luis")
    original = approval_store.consume_plans

    def mutate_external_list(plans, **kw):
        result = original(plans, **kw)
        command[1] = "changed after approval"
        return result

    monkeypatch.setattr(approval_store, "consume_plans", mutate_external_list)
    _, result = router.execute(manifest(), command, workspace=stage["workspace"],
                               artifacts_root=stage["artifacts_root"], run_id="bound", owner="luis")
    assert result.status == "completed"
    assert stage["started"] == [["echo", "original"]]
