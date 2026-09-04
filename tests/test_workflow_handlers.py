"""
tests/test_workflow_handlers.py — what the node types do, end to end.

The engine tests (`test_workflows.py`) prove the run survives a restart. These
prove the node types are honest: a condition that is not met stops its branch
instead of quietly letting it run, a wait comes back rather than waiting twice,
an approval gate reads the answer instead of opening a second card, and every
node type nobody wired up **refuses by name** instead of reporting success.

That last one is the whole reason this file exists. A `deliver` node that
returns `{"delivered": True}` into a handler that does not exist is the worst
possible bug in a workflow engine: the run is green and the email never left.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database as db_mod
from core.database import Base
from src.contracts import WorkflowDefinition
from src.workflows import WorkflowEngine, WorkflowStore, default_handlers, evaluate


@pytest.fixture()
def store(tmp_path, monkeypatch):
    url = "sqlite:///" + (tmp_path / "wfh.db").as_posix()
    engine = create_engine(url, connect_args={"check_same_thread": False})
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal",
                        sessionmaker(autocommit=False, autoflush=False, bind=engine))
    Base.metadata.create_all(bind=engine)
    yield WorkflowStore()
    engine.dispose()


def wf(*nodes, wid="demo.flow"):
    return WorkflowDefinition.parse(
        {"id": wid, "version": "1.0.0", "title": "demo", "nodes": list(nodes)})


def in_seconds(n):
    return (datetime.now(timezone.utc) + timedelta(seconds=n)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


# ── nothing is capable by default ─────────────────────────────────────────

@pytest.mark.parametrize("node_type, config, word", [
    ("deliver", {"to": "ana@example.com"}, "no sender"),
    ("skill", {"skill": "research"}, "no runner"),
    ("artifact_store", {"path": "out.pdf"}, "no store"),
])
def test_a_node_type_nobody_wired_refuses_instead_of_claiming_it_worked(
        store, node_type, config, word):
    """The failure mode this guards against: a green run and no email. Each of
    these reaches outside Faustus, so an unwired one has to fail loudly."""
    d = wf({"id": "n", "type": node_type, "config": config})
    out = WorkflowEngine(default_handlers(), store).advance(
        store.create_run(d)["run_id"])

    assert out["status"] == "failed"
    reason = out["ran"][0]["reason"]
    # Three things the refusal has to do, rather than three exact sentences —
    # the wording moved once already when `skill` gained a wired case, and a
    # test that pins prose fails for the wrong reason.
    assert word in reason, "it does not say WHICH capability is missing"
    assert "did not run" in reason or "nothing was" in reason, \
        "it does not say that nothing happened"
    assert "default_handlers" in reason, "it does not say how to wire it"


def test_a_wired_sender_gets_the_config_and_the_run(store):
    sent = []
    d = wf({"id": "send", "type": "deliver", "config": {"to": "ana@example.com"}})
    handlers = default_handlers(
        deliver=lambda payload, ctx: sent.append((payload, ctx["run_id"]))
        or {"message_id": "m1"})
    run_id = store.create_run(d)["run_id"]
    out = WorkflowEngine(handlers, store).advance(run_id)

    assert out["status"] == "completed"
    assert sent[0][0] == {"to": "ana@example.com"}
    assert sent[0][1] == run_id
    assert out["ran"][0]["result"]["message_id"] == "m1"


# ── conditions ────────────────────────────────────────────────────────────

def test_a_condition_that_is_not_met_stops_its_branch_without_failing_the_run(store):
    """A branch not taken is not a failure. The run completes, and it still
    says which half never happened — a silent skip is indistinguishable from
    a bug."""
    d = wf(
        {"id": "check", "type": "condition",
         "config": {"when": {"left": {"path": "inputs.score"}, "op": "gte",
                             "right": 90}}},
        {"id": "send", "type": "deliver", "needs": ["check"],
         "config": {"to": "ana@x"}},
    )
    sent = []
    handlers = default_handlers(deliver=lambda p, c: sent.append(1) or {})
    run_id = store.create_run(d, inputs={"score": 12})["run_id"]
    out = WorkflowEngine(handlers, store).advance(run_id)

    assert out["status"] == "completed"
    assert sent == [], "the branch behind a false condition ran anyway"
    assert out["not_taken"] == ["send"]
    assert "12" in out["ran"][0]["result"]["reason"]


def test_a_condition_that_is_met_lets_the_branch_through(store):
    d = wf(
        {"id": "check", "type": "condition",
         "config": {"when": {"left": {"path": "inputs.score"}, "op": "gte",
                             "right": 90}}},
        {"id": "send", "type": "deliver", "needs": ["check"], "config": {"to": "ana@x"}},
    )
    sent = []
    handlers = default_handlers(deliver=lambda p, c: sent.append(1) or {})
    out = WorkflowEngine(handlers, store).advance(
        store.create_run(d, inputs={"score": 91})["run_id"])

    assert out["status"] == "completed"
    assert sent == [1]
    assert out["not_taken"] == []


def test_a_condition_can_read_what_an_earlier_node_produced(store):
    d = wf(
        {"id": "count", "type": "manual", "config": {}},
        {"id": "check", "type": "condition", "needs": ["count"],
         "config": {"when": {"left": {"path": "results.count.inputs.n"},
                             "op": "gt", "right": 2}}},
    )
    out = WorkflowEngine(default_handlers(), store).advance(
        store.create_run(d, inputs={"n": 5})["run_id"])
    assert out["status"] == "completed"
    assert out["not_taken"] == []


def test_comparing_things_that_cannot_be_ordered_says_so_instead_of_saying_no(store):
    """`"90" >= 90` is a definition mistake. Answering False would send
    someone to look at their data; naming the type mismatch sends them to the
    line that is wrong."""
    verdict = evaluate({"left": {"path": "inputs.score"}, "op": "gte", "right": 90},
                       {"inputs": {"score": "ninety"}})
    assert verdict["passed"] is False
    assert verdict["error"] == "not_comparable"
    assert "str" in verdict["detail"] and "int" in verdict["detail"]


def test_a_condition_pointing_at_nothing_fails_rather_than_quietly_being_false(store):
    d = wf({"id": "check", "type": "condition",
            "config": {"when": {"left": {"path": "inputs.nope"}, "op": "gt",
                                "right": 1}}})
    out = WorkflowEngine(default_handlers(), store).advance(
        store.create_run(d)["run_id"])
    assert out["status"] == "failed"
    assert "nothing at 'inputs.nope'" in out["ran"][0]["reason"]


def test_a_literal_is_a_literal_and_a_path_is_a_path():
    """`{"path": ...}` is the only way to read from the run. Without that,
    every string in a definition would be an ambiguous lookup."""
    ctx = {"inputs": {"status": "ready"}}
    assert evaluate({"left": {"path": "inputs.status"}, "op": "eq",
                     "right": "ready"}, ctx)["passed"] is True
    # The same words as a literal are compared, not resolved.
    assert evaluate({"left": "inputs.status", "op": "eq",
                     "right": "ready"}, ctx)["passed"] is False


def test_an_unknown_operator_names_the_ones_that_exist():
    verdict = evaluate({"left": 1, "op": "roughly", "right": 1}, {})
    assert "unknown operator" in verdict["error"]
    assert "gte" in verdict["detail"]


# ── waiting ───────────────────────────────────────────────────────────────

def test_a_wait_pauses_once_and_comes_back_rather_than_waiting_again(store):
    """The trap this catches: recomputing `seconds` on the second pass, which
    turns `wait 60s` into a wait that never ends."""
    d = wf(
        {"id": "hold", "type": "wait", "config": {"seconds": 3600}},
        {"id": "send", "type": "deliver", "needs": ["hold"], "config": {"to": "a@b.c"}},
    )
    sent = []
    engine = WorkflowEngine(
        default_handlers(deliver=lambda p, c: sent.append(1) or {}), store)
    run_id = store.create_run(d)["run_id"]

    first = engine.advance(run_id)
    assert first["status"] == "paused"
    assert first["waiting_on"] == "hold"
    assert first["wake_at"] > "2026"
    assert sent == []

    # Called again before the time is up: still waiting, still nothing sent.
    assert engine.advance(run_id)["status"] == "paused"
    assert sent == []

    # The clock catches up. Nothing calls `resume` for a wait — `advance` wakes
    # it, which is what makes a plain timer the whole scheduler.
    states = store.node_runs(run_id)
    store.finish_node(run_id, "hold", status="paused",
                      result={"wake_at": "2020-01-01T00:00:00Z"})
    out = engine.advance(run_id)

    assert out["status"] == "completed"
    assert sent == [1]
    assert states["hold"].status == "paused"


def test_a_wait_for_a_time_that_has_already_passed_does_not_pause_at_all(store):
    d = wf({"id": "hold", "type": "wait",
            "config": {"until": "2020-01-01T00:00:00Z"}})
    out = WorkflowEngine(default_handlers(), store).advance(
        store.create_run(d)["run_id"])
    assert out["status"] == "completed"


def test_a_wait_with_no_deadline_is_refused_rather_than_parked_forever(store):
    d = wf({"id": "hold", "type": "wait", "config": {}})
    out = WorkflowEngine(default_handlers(), store).advance(
        store.create_run(d)["run_id"])
    assert out["status"] == "failed"
    assert "needs `config.seconds`" in out["ran"][0]["reason"]


# ── the human gate ────────────────────────────────────────────────────────

class FakeApprovals:
    """Enough of `approval_store` to test the gate without a database: the
    handler only ever calls `request` and `get`."""

    def __init__(self):
        self.cards = {}
        self.requests = 0
        self.opened_with = []

    def request(self, plan, **kw):
        self.requests += 1
        self.opened_with.append(kw)
        card = {"id": f"apr_{self.requests}", "plan": plan, "status": "pending",
                "reason": "", "decided_by": ""}
        self.cards[card["id"]] = card
        return card

    def get(self, approval_id):
        return self.cards.get(approval_id)


def test_the_gate_opens_one_card_and_waits_on_it(store):
    """Opening a second card on the next pass is how a denial gets lost and a
    person ends up approving the same thing twice."""
    approvals = FakeApprovals()
    d = wf(
        {"id": "gate", "type": "human_approval",
         "config": {"action": "deliver", "detail": "send the report to Ana",
                    "recipients": ["ana@example.com"]}},
        {"id": "send", "type": "deliver", "needs": ["gate"], "config": {"to": "ana@x"}},
    )
    sent = []
    engine = WorkflowEngine(
        default_handlers(approvals=approvals,
                         deliver=lambda p, c: sent.append(1) or {}), store)
    run_id = store.create_run(d)["run_id"]

    first = engine.advance(run_id)
    assert first["status"] == "paused" and first["approval_id"] == "apr_1"
    assert approvals.cards["apr_1"]["plan"]["recipients"] == ["ana@example.com"]

    engine.advance(run_id)                       # a second pass while it waits
    assert approvals.requests == 1, "a second card was opened for the same gate"
    assert sent == []

    approvals.cards["apr_1"]["status"] = "granted"
    approvals.cards["apr_1"]["decided_by"] = "luis"
    out = engine.resume(run_id, "gate")

    assert out["status"] == "completed"
    assert sent == [1]


def test_the_card_belongs_to_whoever_owns_the_run(store):
    """Found by running it against the real server, not by a unit test: the
    gate opened a card with no owner, so it never appeared in the pending list
    the person actually looks at. A question nobody is shown is not a gate."""
    approvals = FakeApprovals()
    d = wf({"id": "gate", "type": "human_approval", "config": {"action": "deliver"}})
    WorkflowEngine(default_handlers(approvals=approvals), store).advance(
        store.create_run(d, owner="luis")["run_id"])
    assert approvals.requests == 1
    assert approvals.opened_with[0]["owner"] == "luis"


def test_waiting_a_long_time_does_not_burn_through_the_attempt_counter(store):
    """A run polled every minute for a week is ordinary. If each poll counted
    as an attempt, the node would pass the contract's ceiling and the run
    would stop being readable at all — and it would be a lie: nothing was
    retried, nobody had answered yet."""
    approvals = FakeApprovals()
    d = wf({"id": "gate", "type": "human_approval", "config": {"action": "deliver"}})
    engine = WorkflowEngine(default_handlers(approvals=approvals), store)
    run_id = store.create_run(d, owner="luis")["run_id"]
    engine.advance(run_id)

    for _ in range(40):
        engine.resume(run_id, "gate")

    state = store.node_runs(run_id)["gate"]
    assert state.status == "paused"
    assert state.attempt == 1, "each poll was counted as a retry"
    assert approvals.requests == 1, "each poll opened another card"

    approvals.cards["apr_1"]["status"] = "granted"
    assert engine.resume(run_id, "gate")["status"] == "completed"


def test_a_denial_stops_the_branch_and_says_who_said_no(store):
    approvals = FakeApprovals()
    d = wf(
        {"id": "gate", "type": "human_approval", "config": {"action": "deliver"}},
        {"id": "send", "type": "deliver", "needs": ["gate"], "config": {"to": "a@b.c"}},
    )
    sent = []
    engine = WorkflowEngine(
        default_handlers(approvals=approvals,
                         deliver=lambda p, c: sent.append(1) or {}), store)
    run_id = store.create_run(d)["run_id"]
    engine.advance(run_id)

    approvals.cards["apr_1"]["status"] = "denied"
    approvals.cards["apr_1"]["reason"] = "wrong recipient"
    out = engine.resume(run_id, "gate")

    assert out["status"] == "failed"
    assert out["failed_nodes"] == ["gate"]
    assert out["never_reached"] == ["send"]
    assert sent == [], "the delivery went ahead after a person said no"


def test_an_approval_that_expired_is_not_a_yes(store):
    approvals = FakeApprovals()
    d = wf({"id": "gate", "type": "human_approval", "config": {"action": "publish"}})
    engine = WorkflowEngine(default_handlers(approvals=approvals), store)
    run_id = store.create_run(d)["run_id"]
    engine.advance(run_id)
    approvals.cards["apr_1"]["status"] = "expired"

    out = engine.resume(run_id, "gate")
    assert out["status"] == "failed"
    assert "expired" in out["ran"][0]["reason"]


def test_the_gate_defaults_its_card_to_something_a_person_can_read(store):
    approvals = FakeApprovals()
    d = wf({"id": "gate", "type": "human_approval", "title": "Publish the post",
            "config": {"action": "publish"}})
    WorkflowEngine(default_handlers(approvals=approvals), store).advance(
        store.create_run(d)["run_id"])
    assert approvals.cards["apr_1"]["plan"]["detail"] == "Publish the post"
