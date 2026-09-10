"""AUTO-02 for workflows — a run's budget and permission set are declared at
`create_run` and enforced by `WorkflowEngine.advance` before every node, not
just at the definition level:

  * budget exhaustion pauses the run (the existing first-class `"paused"`
    state IS the AUTO-02 checkpoint — see `WorkflowEngine._check_budget`)
    instead of running past the declared ceiling.
  * a node outside the declared permission set fails terminally, without
    ever reaching its handler — a policy violation is not a transient
    failure a retry could clear.
  * a run that never declares either behaves exactly as before AUTO-02
    existed.

Mirrors the `store` fixture and `definition()` helper `tests/test_workflows.py`
already uses, so this file exercises the SAME store/engine wiring, only
through `create_run(budget_preset=..., permissions=...)`.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core import database as db_mod
from core.database import Base
from src.contracts import WorkflowDefinition
from src.workflows import WorkflowEngine, WorkflowStore


@pytest.fixture()
def store(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal",
                        sessionmaker(autocommit=False, autoflush=False, bind=engine))
    Base.metadata.create_all(bind=engine)
    yield WorkflowStore()
    engine.dispose()


def definition(**over):
    body = {
        "id": "report.publish", "version": "1.0.0", "title": "Write and send",
        "nodes": [
            {"id": "gather", "type": "skill", "config": {"skill": "research"}},
            {"id": "write", "type": "skill", "needs": ["gather"],
             "config": {"skill": "document.report"}},
            {"id": "send", "type": "deliver", "needs": ["write"],
             "config": {"to": "ana@example.com"}},
        ],
    }
    body.update(over)
    return WorkflowDefinition.parse(body)


def _two_step_definition(**over):
    body = {
        "id": "two.step", "version": "1.0.0", "title": "Two effectful steps",
        "nodes": [
            {"id": "step1", "type": "skill", "config": {}},
            {"id": "step2", "type": "skill", "needs": ["step1"], "config": {}},
        ],
    }
    body.update(over)
    return WorkflowDefinition.parse(body)


# ---------------------------------------------------------------------------
# get_policy — defaults and round trip
# ---------------------------------------------------------------------------

def test_get_policy_defaults_for_a_run_that_never_declared_one(store):
    run_id = store.create_run(definition())["run_id"]
    assert store.get_policy(run_id) == {"budget_preset": "supervised", "permissions": None}


def test_get_policy_round_trips_declared_values(store):
    run_id = store.create_run(definition(), budget_preset="read_only",
                              permissions=["skill"])["run_id"]
    assert store.get_policy(run_id) == {"budget_preset": "read_only", "permissions": ["skill"]}


def test_usage_so_far_counts_only_effectful_terminal_nodes(store):
    handlers = {"skill": lambda n, c: {"ok": True}, "deliver": lambda n, c: {"message_id": "1"}}
    run_id = store.create_run(definition())["run_id"]
    WorkflowEngine(handlers, store).advance(run_id)
    usage = store.usage_so_far(run_id)
    assert usage["tool_calls"] == 3  # gather(skill), write(skill), send(deliver)


# ---------------------------------------------------------------------------
# Budget exhaustion
# ---------------------------------------------------------------------------

def test_budget_exhaustion_pauses_the_run_before_the_next_node_runs(store, monkeypatch):
    from src.autonomy_budget import Budget
    monkeypatch.setattr("src.autonomy_budget.resolve_budget",
                        lambda preset, **kw: Budget(max_tool_calls=1))
    calls = []

    def skill(n, c):
        calls.append(n.id)
        return {"ok": True}

    run_id = store.create_run(_two_step_definition(), budget_preset="supervised")["run_id"]
    events = []
    engine = WorkflowEngine({"skill": skill}, store,
                            on_event=lambda name, data: events.append((name, data)))
    result = engine.advance(run_id)

    assert result["reason"] == "budget_exhausted"
    assert result["status"] == "paused"
    assert calls == ["step1"], "the SECOND node must not run once the budget is exhausted"
    assert any(name == "workflow.budget_exhausted" for name, _ in events)
    assert store.get_run(run_id)["run"].status == "paused"


def test_no_declared_budget_preset_behaves_exactly_as_before(store):
    """Backward compat: a run created without budget_preset/permissions is
    checked against the real `supervised` default, which is generous enough
    that a two-node workflow completes exactly as it always did."""
    calls = []

    def skill(n, c):
        calls.append(n.id)
        return {"ok": True}

    run_id = store.create_run(_two_step_definition())["run_id"]
    result = WorkflowEngine({"skill": skill}, store).advance(run_id)
    assert result["status"] == "completed"
    assert calls == ["step1", "step2"]


# ---------------------------------------------------------------------------
# Permission denial
# ---------------------------------------------------------------------------

def test_a_node_outside_declared_permissions_fails_without_running_its_handler(store):
    ran_deliver = []

    def deliver(n, c):
        ran_deliver.append(n.id)
        return {"message_id": "x"}

    handlers = {"skill": lambda n, c: {"ok": True}, "deliver": deliver}
    run_id = store.create_run(definition(), permissions=["skill"])["run_id"]
    events = []
    result = WorkflowEngine(handlers, store,
                            on_event=lambda name, data: events.append((name, data))).advance(run_id)

    assert result["reason"] == "failed"
    assert "send" in result["failed_nodes"]
    assert ran_deliver == [], "a denied node's handler must never execute"

    send_state = store.node_runs(run_id)["send"]
    assert send_state.status == "failed"
    assert "policy_permission_denied" in (send_state.reason or "")
    assert any(name == "workflow.node" and data.get("node") == "send"
              and data.get("status") == "failed" for name, data in events)


def test_declared_permissions_covering_every_node_type_runs_normally(store):
    """Declaring permissions is only a CEILING: naming exactly the node types
    the definition already uses changes nothing about the outcome."""
    ran = []

    def skill(n, c):
        ran.append(n.id)
        return {"ok": True}

    def deliver(n, c):
        ran.append(n.id)
        return {"message_id": "z"}

    handlers = {"skill": skill, "deliver": deliver}
    run_id = store.create_run(
        definition(), permissions=["skill", "deliver", "not_used_by_this_definition"],
    )["run_id"]
    result = WorkflowEngine(handlers, store).advance(run_id)
    assert result["status"] == "completed"
    assert ran == ["gather", "write", "send"]


def test_no_declared_permissions_behaves_exactly_as_before(store):
    ran = []
    handlers = {
        "skill": lambda n, c: (ran.append(n.id), {"ok": True})[1],
        "deliver": lambda n, c: (ran.append(n.id), {"message_id": "z"})[1],
    }
    run_id = store.create_run(definition())["run_id"]
    result = WorkflowEngine(handlers, store).advance(run_id)
    assert result["status"] == "completed"
    assert ran == ["gather", "write", "send"]
