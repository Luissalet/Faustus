"""The workflow core (src/workflows) — and its stop condition.

The masterplan's rule for this phase: *do not advance if restarting repeats a
publication, a render or an email.* So the test that matters most is the one
that kills the engine mid-node and runs it again, counting the side effects
with a list the handler appends to.

Everything else here is about the same idea from a different angle: a retry
that reaches outside is refused at the contract level unless someone said the
effect can take it, a paused run is not a failed one, and a definition edited
while a run is paused does not change what the rest of that run does.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database as db_mod
from core.database import Base
from src.contracts import ContractError, WorkflowDefinition
from src.workflows import WorkflowEngine, WorkflowStore, ready_nodes


@pytest.fixture()
def store(tmp_path, monkeypatch):
    url = "sqlite:///" + (tmp_path / "wf.db").as_posix()
    engine = create_engine(url, connect_args={"check_same_thread": False})
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


# ── the stop condition ─────────────────────────────────────────────────────

def test_a_restart_mid_run_does_not_send_the_email_twice(store):
    """The rule this phase exists for. The engine is killed after the delivery
    node has been claimed and run, and then started again from the same rows —
    counting the actual side effect, not the status."""
    sent = []

    def deliver(node, ctx):
        sent.append(node.config["to"])
        return {"message_id": f"msg-{len(sent)}"}

    handlers = {"skill": lambda n, c: {"ok": True}, "deliver": deliver}
    run_id = store.create_run(definition())["run_id"]

    first = WorkflowEngine(handlers, store).advance(run_id)
    assert first["status"] == "completed"
    assert sent == ["ana@example.com"]

    # A whole new engine, as after a restart: same rows, same run.
    second = WorkflowEngine(handlers, WorkflowStore()).advance(run_id)
    assert second["reason"] == "already_completed"
    assert sent == ["ana@example.com"], "the restart sent it again"


def test_a_crash_between_claiming_and_finishing_does_not_redo_the_work(store):
    """The nastier case: the process dies after the node acted but before its
    result was written. The claim is already in the table, so the next pass
    reads it instead of acting again."""
    sent = []

    def deliver(node, ctx):
        sent.append("boom")
        raise SystemExit("power cut")          # after the effect, before the write

    d = definition(nodes=[{"id": "send", "type": "deliver",
                           "config": {"to": "ana@example.com"}}])
    run_id = store.create_run(d)["run_id"]
    engine = WorkflowEngine({"deliver": deliver}, store)
    with pytest.raises(SystemExit):
        engine.advance(run_id)
    assert sent == ["boom"]

    # Restart: a different engine, and a handler that would send again.
    again = WorkflowEngine({"deliver": lambda n, c: sent.append("again") or {}},
                           WorkflowStore())
    result = again.advance(run_id)
    assert sent == ["boom"], "the second pass re-ran a node that had already acted"
    assert result["ran"][0]["reason"] == "already_attempted"


def test_a_redelivered_trigger_is_one_run_not_two(store):
    d = definition()
    first = store.create_run(d, dedupe_key="webhook-evt-42")
    second = store.create_run(d, dedupe_key="webhook-evt-42")
    assert first["created"] is True
    assert second["created"] is False
    assert second["reason"] == "duplicate_trigger"
    assert second["run_id"] == first["run_id"]


# ── the contract refuses the dangerous definition ─────────────────────────

def test_retrying_something_that_reaches_outside_needs_saying_so():
    with pytest.raises(ContractError) as err:
        definition(nodes=[{"id": "send", "type": "deliver", "max_attempts": 3,
                           "config": {"to": "ana@example.com"}}])
    assert "sends the email again" in err.value.message

    # With the author's word that the effect can take it, it is allowed.
    ok = definition(nodes=[{"id": "send", "type": "deliver", "max_attempts": 3,
                            "config": {"to": "a@b.c", "idempotent": True}}])
    assert ok.nodes[0].max_attempts == 3

    # And a node that stays inside Faustus retries freely.
    inside = definition(nodes=[{"id": "check", "type": "condition",
                                "max_attempts": 4, "config": {}}])
    assert inside.nodes[0].max_attempts == 4


def test_a_definition_that_could_never_start_is_refused_with_the_circle():
    with pytest.raises(ContractError) as err:
        definition(nodes=[
            {"id": "a", "type": "condition", "needs": ["b"]},
            {"id": "b", "type": "condition", "needs": ["a"]},
        ])
    assert "circle" in err.value.message
    assert "a" in err.value.message and "b" in err.value.message


def test_a_dependency_on_a_node_that_does_not_exist_is_named():
    with pytest.raises(ContractError) as err:
        definition(nodes=[{"id": "a", "type": "condition", "needs": ["ghost"]}])
    assert "'ghost'" in err.value.message or "ghost" in err.value.message
    assert "no node in this workflow defines" in err.value.message


def test_a_node_cannot_depend_on_itself():
    with pytest.raises(ContractError) as err:
        definition(nodes=[{"id": "a", "type": "condition", "needs": ["a"]}])
    assert "cannot depend on itself" in err.value.message


# ── paused is not failed ───────────────────────────────────────────────────

def test_a_run_waiting_on_a_person_is_paused_and_says_on_what(store):
    def approve(node, ctx):
        return {"status": "paused", "approval_id": "apr_demo",
                "reason": "needs a human"}

    d = definition(nodes=[
        {"id": "write", "type": "skill", "config": {}},
        {"id": "gate", "type": "human_approval", "needs": ["write"], "config": {}},
        {"id": "send", "type": "deliver", "needs": ["gate"], "config": {"to": "a@b.c"}},
    ])
    sent = []
    engine = WorkflowEngine({"skill": lambda n, c: {"ok": True},
                             "human_approval": approve,
                             "deliver": lambda n, c: sent.append(1) or {}}, store)
    run_id = store.create_run(d)["run_id"]
    out = engine.advance(run_id)

    assert out["status"] == "paused"
    assert out["waiting_on"] == "gate"
    assert out["approval_id"] == "apr_demo"
    assert sent == [], "the node after the gate ran before the gate was answered"

    # Calling advance again while it waits changes nothing and sends nothing.
    again = engine.advance(run_id)
    assert again["status"] == "paused" and sent == []


def test_once_the_person_answers_the_run_carries_on(store):
    answered = {"yes": False}

    def gate(node, ctx):
        if answered["yes"]:
            return {"approved": True}
        return {"status": "paused", "approval_id": "apr_demo"}

    sent = []
    d = definition(nodes=[
        {"id": "gate", "type": "human_approval", "config": {}},
        {"id": "send", "type": "deliver", "needs": ["gate"], "config": {"to": "a@b.c"}},
    ])
    engine = WorkflowEngine({"human_approval": gate,
                             "deliver": lambda n, c: sent.append(1) or {"sent": True}},
                            store)
    run_id = store.create_run(d)["run_id"]
    assert engine.advance(run_id)["status"] == "paused"

    answered["yes"] = True
    out = engine.resume(run_id, "gate")
    assert out["status"] == "completed"
    assert sent == [1]


def test_pausing_without_saying_what_for_is_refused(store):
    d = definition(nodes=[{"id": "gate", "type": "human_approval", "config": {}}])
    engine = WorkflowEngine({"human_approval": lambda n, c: {"status": "paused"}}, store)
    run_id = store.create_run(d)["run_id"]
    out = engine.advance(run_id)
    assert out["status"] == "failed"
    assert "without an approval id" in str(out["ran"][0]["reason"])


# ── failure stops the branch, and says which ──────────────────────────────

def test_a_failure_stops_what_depended_on_it_and_names_what_never_ran(store):
    d = definition(nodes=[
        {"id": "gather", "type": "skill", "config": {}},
        {"id": "write", "type": "skill", "needs": ["gather"], "config": {}},
        {"id": "send", "type": "deliver", "needs": ["write"], "config": {"to": "a@b.c"}},
        {"id": "log", "type": "artifact_store", "config": {}},   # independent
    ])
    done = []
    engine = WorkflowEngine({
        "skill": lambda n, c: ({"status": "failed", "reason": "the model refused"}
                               if n.id == "gather" else done.append(n.id) or {}),
        "deliver": lambda n, c: done.append("send") or {},
        "artifact_store": lambda n, c: done.append("log") or {},
    }, store)
    run_id = store.create_run(d)["run_id"]
    out = engine.advance(run_id)

    assert out["status"] == "failed"
    assert out["failed_nodes"] == ["gather"]
    assert set(out["never_reached"]) == {"write", "send"}
    assert done == ["log"], "an unrelated branch was stopped by someone else's failure"


def test_a_failure_the_author_allowed_does_not_stop_the_branch(store):
    """`continue_on_failure` is the escape hatch for the step that is allowed
    to not work — the optional enrichment, the nice-to-have screenshot. The
    branch carries on and the run is completed, but the failure is still
    named: a green run that quietly swallowed a broken step is worse than a
    red one."""
    d = definition(nodes=[
        {"id": "enrich", "type": "skill", "config": {},
         "continue_on_failure": True},
        {"id": "write", "type": "skill", "needs": ["enrich"], "config": {}},
        {"id": "send", "type": "deliver", "needs": ["write"], "config": {"to": "a@b.c"}},
    ])
    done = []
    engine = WorkflowEngine({
        "skill": lambda n, c: ({"status": "failed", "reason": "no data source"}
                               if n.id == "enrich" else done.append(n.id) or {}),
        "deliver": lambda n, c: done.append("send") or {},
    }, store)
    out = engine.advance(store.create_run(d)["run_id"])

    assert out["status"] == "completed"
    assert done == ["write", "send"], "a tolerated failure stopped the branch anyway"
    assert out["tolerated_failures"] == ["enrich"]


def test_a_tolerated_failure_does_not_hide_a_real_one(store):
    """Two failures, one allowed and one not. The run is failed, and the two
    are reported apart: mixing them would let the flag launder a real fault."""
    d = definition(nodes=[
        {"id": "enrich", "type": "skill", "config": {}, "continue_on_failure": True},
        {"id": "gather", "type": "skill", "config": {}},
        {"id": "send", "type": "deliver", "needs": ["gather"], "config": {"to": "a@b.c"}},
    ])
    engine = WorkflowEngine({
        "skill": lambda n, c: {"status": "failed", "reason": "no"},
        "deliver": lambda n, c: {},
    }, store)
    out = engine.advance(store.create_run(d)["run_id"])

    assert out["status"] == "failed"
    assert out["failed_nodes"] == ["gather"]
    assert out["tolerated_failures"] == ["enrich"]
    assert out["never_reached"] == ["send"]


def test_a_node_type_with_no_handler_fails_by_name_rather_than_hanging(store):
    d = definition(nodes=[{"id": "wat", "type": "webhook", "config": {}}])
    out = WorkflowEngine({}, store).advance(store.create_run(d)["run_id"])
    assert out["status"] == "failed"
    assert "no handler for node type 'webhook'" in out["ran"][0]["reason"]


def test_a_handler_that_raises_does_not_take_the_run_with_it(store):
    def boom(node, ctx):
        raise ValueError("something in the node")

    d = definition(nodes=[{"id": "a", "type": "condition", "config": {}}])
    out = WorkflowEngine({"condition": boom}, store).advance(
        store.create_run(d)["run_id"])
    assert out["status"] == "failed"
    assert "ValueError: something in the node" in out["ran"][0]["reason"]


def test_a_retry_that_is_allowed_gets_a_second_attempt(store):
    tries = []

    def flaky(node, ctx):
        tries.append(ctx["attempt"])
        if len(tries) < 3:
            return {"status": "failed", "reason": "not yet"}
        return {"ok": True}

    d = definition(nodes=[{"id": "a", "type": "condition", "max_attempts": 3,
                           "config": {}}])
    engine = WorkflowEngine({"condition": flaky}, store)
    out = engine.advance(store.create_run(d)["run_id"])
    assert tries == [1, 2, 3]
    assert out["status"] == "completed"


# ── the version a run started under ───────────────────────────────────────

def test_editing_the_definition_does_not_change_a_run_already_going(store):
    """Same rule as an approval naming a skill version: what a run does is
    fixed when it starts, not looked up as it goes."""
    original = definition(nodes=[
        {"id": "gate", "type": "human_approval", "config": {}},
        {"id": "send", "type": "deliver", "needs": ["gate"], "config": {"to": "ana@x"}},
    ])
    run_id = store.create_run(original)["run_id"]
    engine = WorkflowEngine({
        "human_approval": lambda n, c: {"status": "paused", "approval_id": "apr"},
        "deliver": lambda n, c: {"to": n.config["to"]},
    }, store)
    assert engine.advance(run_id)["status"] == "paused"

    # Somebody edits the workflow while it waits — a new recipient.
    definition(nodes=[
        {"id": "gate", "type": "human_approval", "config": {}},
        {"id": "send", "type": "deliver", "needs": ["gate"], "config": {"to": "mallory@x"}},
    ])
    engine.handlers["human_approval"] = lambda n, c: {"approved": True}
    out = engine.resume(run_id, "gate")

    assert out["status"] == "completed"
    delivered = next(r for r in out["ran"] if r["node_id"] == "send")
    assert delivered["result"]["to"] == "ana@x"


def test_the_stored_run_carries_the_definition_it_started_with(store):
    run_id = store.create_run(definition())["run_id"]
    loaded = store.get_run(run_id)
    assert loaded["definition"].fingerprint() == definition().fingerprint()
    assert loaded["run"].workflow_version == "1.0.0"


# ── the graph reader ──────────────────────────────────────────────────────

def test_blocked_nodes_are_returned_so_nothing_looks_finished_when_it_is_stuck(store):
    d = definition()
    run_id = store.create_run(d)["run_id"]
    engine = WorkflowEngine({"skill": lambda n, c: (
        {"status": "failed", "reason": "no"} if n.id == "gather" else {})}, store)
    engine.advance(run_id)

    runnable, blocked = ready_nodes(d, store.node_runs(run_id))
    assert runnable == []
    assert [n.id for n in blocked] == ["write"]   # `send` is not its turn yet


def test_a_run_that_does_not_exist_is_said_so_rather_than_crashing(store):
    out = WorkflowEngine({}, store).advance("wfr_nope")
    assert out == {"ok": False, "reason": "not_found", "run_id": "wfr_nope"}
