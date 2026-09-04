"""The wiring: a run that raises approval cards does not start without them.

Everything before this built the pieces — a manifest that knows which cards it
raises, a store that knows whether one covers a plan. This is the file that
makes the gate load-bearing, and the check lives in `execution_router.execute`
rather than at each call site because a gate you have to remember to call is a
gate that will be forgotten once.

The last test is the one that matters most: a card granted for one plan does
not cover the same skill with one more secret. That is the drift the whole
approval design exists to catch, and here it stops a real run.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database as db_mod
from core.database import Base
from src import approval_store, capability_registry as registry, execution_router as router
from src.contracts import SkillManifest


def manifest(**over):
    # A document, not a video: `docker_workspace` cannot make video, so a
    # video manifest would be refused by the router before the gate is
    # reached — which would test the wrong thing.
    body = {
        "id": "report.publish", "version": "1.0.0", "title": "Publish the report",
        "outputs": {"report": "artifact:document"},
        "permissions": {"backends": ["docker_workspace"], "max_seconds": 60},
        "approval": {"required_when": ["publish"]},
    }
    body.update(over)
    return SkillManifest.parse(body)


@pytest.fixture()
def stage(tmp_path, monkeypatch):
    url = "sqlite:///" + (tmp_path / "gate.db").as_posix()
    engine = create_engine(url, connect_args={"check_same_thread": False})
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal",
                        sessionmaker(autocommit=False, autoflush=False, bind=engine))
    Base.metadata.create_all(bind=engine)

    from src.capability_registry import Observation
    monkeypatch.setattr(registry, "_probe_cache", {})
    monkeypatch.setattr(registry, "_probe_docker", lambda stamp: Observation(
        "docker_workspace", "available", "stubbed for the gate test", stamp))

    ws = tmp_path / "ws"
    ws.mkdir()
    started = []

    class _Backend:
        id = "docker_workspace"

        def run(self, spec, command, **kw):
            started.append(command)
            from src.contracts import ExecutionResult
            from src.contracts.base import now_iso
            return ExecutionResult.parse({
                "run_id": kw.get("run_id", ""), "backend": self.id,
                "status": "completed", "exit_code": 0,
                "started_at": now_iso(), "ended_at": now_iso()})

    monkeypatch.setattr(router.execution_backends, "build", lambda bid, **kw: _Backend())
    yield {"workspace": str(ws), "artifacts_root": str(tmp_path / "runs"), "started": started}
    engine.dispose()


def _run(stage, man, **kw):
    return router.execute(man, ["echo", "hi"], workspace=stage["workspace"],
                          artifacts_root=stage["artifacts_root"],
                          run_id=kw.pop("run_id", "r1"), owner="luis", **kw)


def test_a_run_that_raises_a_card_does_not_start_without_it(stage):
    decision, result = _run(stage, manifest())
    assert decision.ok is True                     # the routing was fine
    assert result.status == "refused"
    assert "waiting on approval for publish" in result.reason
    assert stage["started"] == [], "the backend was reached before the card was granted"


def test_the_refusal_hands_back_a_card_the_user_can_actually_grant(stage):
    _, result = _run(stage, manifest())
    pending = approval_store.pending(owner="luis")
    assert len(pending) == 1
    assert pending[0].plan.action == "publish"
    assert pending[0].plan.skill_id == "report.publish"
    assert pending[0].id in result.reason          # the card id is in the message


def test_once_a_person_grants_it_the_run_goes_ahead(stage):
    _run(stage, manifest())
    card = approval_store.pending(owner="luis")[0]
    approval_store.decide(card.id, granted=True, by="luis")

    decision, result = _run(stage, manifest(), run_id="r2")
    assert result.status == "completed"
    assert stage["started"] == [["echo", "hi"]]


def test_a_manifest_that_raises_nothing_is_untouched_by_the_gate(stage):
    quiet = manifest(outputs={"notes": "artifact:text"}, approval={"required_when": []})
    decision, result = _run(stage, quiet)
    assert result.status == "completed"
    assert approval_store.pending(owner="luis") == []


def test_one_more_secret_and_the_granted_card_no_longer_covers_it(stage):
    """The drift the whole approval design exists to catch, stopping a real run."""
    _run(stage, manifest())
    card = approval_store.pending(owner="luis")[0]
    approval_store.decide(card.id, granted=True, by="luis")
    assert _run(stage, manifest(), run_id="r2")[1].status == "completed"

    greedier = manifest(permissions={"backends": ["docker_workspace"], "max_seconds": 60,
                                     "secrets": ["youtube"]})
    decision, result = _run(stage, greedier, run_id="r3")
    assert result.status == "refused"
    assert "plan_changed" in result.reason
    assert "secret_names" in result.reason or "permissions" in result.reason
    assert stage["started"] == [["echo", "hi"]]     # still only the approved run


def test_an_undeclared_card_earned_by_the_permissions_is_enforced_too(stage):
    """`implied_approvals` is not decoration: asking for the network raises a
    card the manifest never declared, and the gate honours it."""
    sneaky = manifest(approval={"required_when": []},
                      permissions={"backends": ["docker_workspace"], "network": True,
                                   "max_seconds": 60})
    assert sneaky.approval_required_when == ()
    assert "network" in sneaky.effective_approvals()
    _, result = _run(stage, sneaky)
    assert result.status == "refused"
    assert "waiting on approval for network" in result.reason


def test_the_gate_can_be_stood_down_explicitly_and_only_explicitly(stage):
    decision, result = _run(stage, manifest(), require_approval=False)
    assert result.status == "completed"
    assert approval_store.pending(owner="luis") == []
