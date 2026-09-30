"""The `loop` node: a bounded, resumable repeat of a few sibling nodes.

What is pinned here:

* the contract refuses every way a loop could be unbounded or ambiguous, and
  still refuses a cycle in `needs`;
* a loop runs through the real engine and the real store, pass by pass, with
  its `until` condition, its ceilings, its pause-and-extend path and its
  cancellation;
* a process that dies between two passes (or inside one) comes back to the
  SAME iteration, never to iteration 1, and never repeats an effect it cannot
  prove did not land;
* every tool that reads a definition (lint, estimate, preflight, simulate,
  mermaid) knows what a loop is.

A crash is simulated the way the other workflow suites do it: a handler raises
`SystemExit` (which no `except Exception` swallows), then the expired lease is
recovered with a clock far in the future.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database as db_mod
from core.database import Base
from src.contracts import ContractError, WorkflowDefinition
from src.contracts.workflow_iteration import loop_effect_idempotency_key
from src.workflows import WorkflowEngine, WorkflowStore, default_handlers

FAR_FUTURE = "2999-01-01T00:00:00Z"
START = {"id": "start", "type": "manual"}


@pytest.fixture()
def store(tmp_path, monkeypatch):
    url = "sqlite:///" + (tmp_path / "wf_loop.db").as_posix()
    engine = create_engine(url, connect_args={"check_same_thread": False})
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal",
                        sessionmaker(autocommit=False, autoflush=False, bind=engine))
    Base.metadata.create_all(bind=engine)
    yield WorkflowStore()
    engine.dispose()


def parse(nodes: List[Dict[str, Any]], wid: str = "loop.flow") -> WorkflowDefinition:
    return WorkflowDefinition.parse({"id": wid, "version": "1.0.0", "title": "Loop flow", "nodes": nodes})


def step(node_id: str = "step", skill: str = "work.step", **extra) -> Dict[str, Any]:
    return {"id": node_id, "type": "skill", "needs": ["start"], "config": {"skill": skill}, **extra}


def loop_flow(body=None, *, budget=None, until=None, loop_extra=None, tail=True, wid="loop.flow"):
    body = body if body is not None else [step()]
    cfg: Dict[str, Any] = {"body": [n["id"] for n in body], "budget": budget or {"max_iterations": 3}}
    if until is not None:
        cfg["until"] = until
    nodes = [START, {"id": "lp", "type": "loop", "needs": ["start"], "config": cfg, **(loop_extra or {})}, *body]
    if tail:
        nodes.append({"id": "after", "type": "skill", "needs": ["lp"], "config": {"skill": "after.run"}})
    return parse(nodes, wid)


class Skills:
    """A `skill` runner that records each call with the loop iteration it saw."""

    def __init__(self, produce=None, crash_on=None, fail_on=None):
        self.calls: List[Dict[str, Any]] = []
        self.produce = produce
        self.crash_on = crash_on          # (node_id, iteration)
        self.fail_on = fail_on

    def __call__(self, node, context):
        loop = context.get("loop") or {}
        self.calls.append({"node": node.id, "iteration": loop.get("iteration"),
                           "key": context.get("idempotency_key"), "previous": context.get("previous"),
                           "loop": loop})
        if self.crash_on == (node.id, loop.get("iteration")):
            self.crash_on = None
            raise SystemExit("the process died here")
        if self.fail_on == (node.id, loop.get("iteration")):
            return {"status": "failed", "reason": "the step broke"}
        if self.produce:
            return self.produce(node, context)
        return {"n": loop.get("iteration"), "ran": node.id}

    def iterations(self, node_id="step"):
        return [c["iteration"] for c in self.calls if c["node"] == node_id]


def run(store, definition, skills, *, permissions=None, **create):
    run_id = store.create_run(definition, owner="luis", inputs=create.pop("inputs", {}),
                              permissions=permissions, **create)["run_id"]
    return run_id, WorkflowEngine(default_handlers(skill=skills), store).advance(run_id)


# ═════════════════════════ contract ═════════════════════════════════════

def nodes_with(loop_cfg, *extra):
    return [START, {"id": "lp", "type": "loop", "needs": ["start"], "config": loop_cfg},
            step(), *extra]


@pytest.mark.parametrize("cfg,needle", [
    ({"body": ["step"]}, "budget"),
    ({"body": ["step"], "budget": {}}, "max_iterations"),
    ({"body": ["step"], "budget": {"max_iterations": 0}}, "max_iterations"),
    ({"body": ["step"], "budget": {"max_iterations": 10001}}, "max_iterations"),
    ({"body": [], "budget": {"max_iterations": 2}}, "at least one"),
    ({"body": ["ghost"], "budget": {"max_iterations": 2}}, "no sibling node defines"),
    ({"body": ["lp"], "budget": {"max_iterations": 2}}, "cannot contain itself"),
    ({"body": ["step"], "budget": {"max_iterations": 2, "max_tokens": 5000}}, "not metered"),
    ({"body": ["step"], "budget": {"max_iterations": 2}, "until": {"left": 1, "op": "sideways"}}, "op"),
    ({"body": ["step"], "budget": {"max_iterations": 2}, "forever": True}, "forever"),
])
def test_a_loop_must_be_bounded_and_well_formed(cfg, needle):
    with pytest.raises(ContractError) as err:
        parse(nodes_with(cfg))
    assert needle in str(err.value)


def test_a_well_formed_loop_parses_and_round_trips():
    d = parse(nodes_with({"body": ["step"], "budget": {"max_iterations": 4, "on_exhausted": "fail"},
                          "until": {"left": {"path": "loop.iteration"}, "op": "gte", "right": 3}}))
    assert WorkflowDefinition.parse(d.to_dict()).fingerprint() == d.fingerprint()


@pytest.mark.parametrize("body_type", ["human_approval", "wait", "wait_until", "wait_for_event",
                                       "manual", "webhook"])
def test_a_body_may_not_wait_on_a_person_a_clock_or_an_event(body_type):
    nodes = [START, {"id": "lp", "type": "loop", "needs": ["start"],
                     "config": {"body": ["x"], "budget": {"max_iterations": 2}}},
             {"id": "x", "type": body_type, "needs": ["start"], "config": {}}]
    with pytest.raises(ContractError) as err:
        parse(nodes)
    assert "loop body may only use" in str(err.value)


def test_a_loop_inside_a_loop_is_refused():
    nodes = [START,
             {"id": "outer", "type": "loop", "needs": ["start"],
              "config": {"body": ["inner"], "budget": {"max_iterations": 2}}},
             {"id": "inner", "type": "loop", "needs": ["start"],
              "config": {"body": ["step"], "budget": {"max_iterations": 2}}},
             step()]
    with pytest.raises(ContractError) as err:
        parse(nodes)
    assert "loop body may only use" in str(err.value)


def test_a_node_belongs_to_at_most_one_loop():
    nodes = [START,
             {"id": "a", "type": "loop", "needs": ["start"], "config": {"body": ["step"], "budget": {"max_iterations": 2}}},
             {"id": "b", "type": "loop", "needs": ["start"], "config": {"body": ["step"], "budget": {"max_iterations": 2}}},
             step()]
    with pytest.raises(ContractError) as err:
        parse(nodes)
    assert "already in the body of loop" in str(err.value)


def test_a_body_node_may_only_read_its_siblings_and_what_the_loop_waits_for():
    other = {"id": "other", "type": "skill", "needs": ["start"], "config": {"skill": "x.y"}}
    body = {"id": "step", "type": "skill", "needs": ["other"], "config": {"skill": "x.y"}}
    nodes = [START, {"id": "lp", "type": "loop", "needs": ["start"],
                     "config": {"body": ["step"], "budget": {"max_iterations": 2}}}, other, body]
    with pytest.raises(ContractError) as err:
        parse(nodes)
    assert "neither" in str(err.value)
    # the same node is fine once the loop itself waits for it
    nodes[1] = {"id": "lp", "type": "loop", "needs": ["other"],
                "config": {"body": ["step"], "budget": {"max_iterations": 2}}}
    parse(nodes)


def test_nothing_outside_a_loop_may_wait_on_its_body():
    nodes = [START, {"id": "lp", "type": "loop", "needs": ["start"],
                     "config": {"body": ["step"], "budget": {"max_iterations": 2}}}, step(),
             {"id": "peek", "type": "skill", "needs": ["step"], "config": {"skill": "x.y"}}]
    with pytest.raises(ContractError) as err:
        parse(nodes)
    assert "depend on the loop" in str(err.value)


def test_a_loop_cannot_wait_on_its_own_body():
    nodes = [START, {"id": "lp", "type": "loop", "needs": ["step"],
                     "config": {"body": ["step"], "budget": {"max_iterations": 2}}}, step()]
    with pytest.raises(ContractError):
        parse(nodes)


def test_a_cycle_in_needs_is_still_refused_and_a_body_cannot_wait_on_its_loop():
    with pytest.raises(ContractError) as err:
        parse([{"id": "a", "type": "skill", "needs": ["b"], "config": {"skill": "x.y"}},
               {"id": "b", "type": "skill", "needs": ["a"], "config": {"skill": "x.y"}}])
    assert "circle" in str(err.value)
    nodes = [START, {"id": "lp", "type": "loop", "needs": ["start"],
                     "config": {"body": ["step"], "budget": {"max_iterations": 2}}},
             {"id": "step", "type": "skill", "needs": ["lp"], "config": {"skill": "x.y"}}]
    with pytest.raises(ContractError):
        parse(nodes)


# ═════════════════════════ running ══════════════════════════════════════

def test_a_loop_without_until_runs_exactly_max_iterations_and_completes(store):
    skills = Skills()
    run_id, result = run(store, loop_flow(budget={"max_iterations": 3}), skills)
    assert result["status"] == "completed"
    assert skills.iterations() == [1, 2, 3] and skills.iterations("after") == [None]
    out = store.node_runs(run_id)["lp"].result
    assert out["iterations"] == 3 and out["exited_by"] == "max_iterations" and out["until_met"] is False
    assert out["results"]["step"] == {"n": 3, "ran": "step"}
    assert [h["status"] for h in out["history"]] == ["completed"] * 3
    rows = store.iteration_rows(run_id, "lp")
    assert [r["iteration"] for r in rows] == [1, 2, 3] and all(r["status"] == "completed" for r in rows)


def test_until_stops_the_loop_early_and_downstream_reads_the_last_result(store):
    until = {"left": {"path": "loop.results.step.n"}, "op": "gte", "right": 2}
    skills = Skills()
    run_id, result = run(store, loop_flow(budget={"max_iterations": 9}, until=until), skills)
    assert result["status"] == "completed" and skills.iterations() == [1, 2]
    out = store.node_runs(run_id)["lp"].result
    assert out["exited_by"] == "until" and out["until_met"] is True and out["iterations"] == 2
    seen = {}

    def after(node, context):
        seen["last"] = context["results"]["lp"]["results"]["step"]
        return {}

    skills2 = Skills(produce=lambda n, c: after(n, c) if n.id == "after" else {"n": c["loop"]["iteration"]})
    run(store, loop_flow(budget={"max_iterations": 9}, until=until, wid="loop.flow2"), skills2)
    assert seen["last"] == {"n": 2}


def test_body_nodes_see_the_iteration_and_the_previous_pass(store):
    until = {"left": {"path": "loop.iteration"}, "op": "gte", "right": 3}
    skills = Skills(produce=lambda n, c: {"n": c["loop"]["iteration"], "saw": dict(c["loop"]["previous"])}
                    if n.id == "step" else {})
    run(store, loop_flow(budget={"max_iterations": 5}, until=until), skills)
    first, second, third = [c for c in skills.calls if c["node"] == "step"]
    assert first["loop"]["first"] is True and first["loop"]["previous"] == {}
    assert second["loop"]["previous"] == {"step": {"n": 1, "saw": {}}}
    assert third["loop"]["max_iterations"] == 5


def test_body_nodes_run_in_needs_order_and_read_each_others_output(store):
    body = [{"id": "second", "type": "skill", "needs": ["first"], "config": {"skill": "x.second"}},
            {"id": "first", "type": "skill", "needs": ["start"], "config": {"skill": "x.first"}}]
    order: List[str] = []

    def produce(node, context):
        order.append(node.id)
        if node.id == "second":
            assert context["results"]["first"] == {"who": "first", "i": context["loop"]["iteration"]}
        return {"who": node.id, "i": context["loop"]["iteration"]}

    run_id, result = run(store, loop_flow(body, budget={"max_iterations": 2}, tail=False), Skills(produce=produce))
    assert result["status"] == "completed"
    assert order == ["first", "second", "first", "second"]


def test_the_body_is_never_a_top_level_node(store):
    skills = Skills()
    run_id, result = run(store, loop_flow(budget={"max_iterations": 2}), skills)
    assert "step" not in store.node_runs(run_id)
    assert result["ran"] and all(r["node_id"] != "step" for r in result["ran"])


def test_body_idempotency_keys_are_per_iteration_and_stable(store):
    skills = Skills()
    d = loop_flow(budget={"max_iterations": 3})
    run_id, _ = run(store, d, skills)
    keys = [c["key"] for c in skills.calls if c["node"] == "step"]
    assert len(set(keys)) == 3
    node = d.node("step")
    expected = loop_effect_idempotency_key(workflow_run_id=run_id, node_id="step", iteration=2,
                                           config=node.config, inputs={"start": store.node_runs(run_id)["start"].result})
    assert keys[1] == expected


# ═════════════════════════ failing, skipping, branching ═════════════════

def test_a_failing_body_node_fails_the_loop_and_names_the_iteration(store):
    skills = Skills(fail_on=("step", 2))
    run_id, result = run(store, loop_flow(budget={"max_iterations": 5}), skills)
    assert result["status"] == "failed" and result["failed_nodes"] == ["lp"]
    reason = store.node_runs(run_id)["lp"].reason
    assert "iteration 2" in reason and "step" in reason and "the step broke" in reason
    assert skills.iterations("after") == []
    rows = store.iteration_rows(run_id, "lp")
    assert [r["status"] for r in rows] == ["completed", "failed"]


def test_a_tolerated_body_failure_lets_the_pass_carry_on(store):
    body = [step(continue_on_failure=True), step("tail", "x.tail")]
    skills = Skills(fail_on=("step", 1))
    run_id, result = run(store, loop_flow(body, budget={"max_iterations": 2}), skills)
    assert result["status"] == "completed"
    assert skills.iterations("tail") == [1, 2]


def test_a_skipped_condition_in_the_body_skips_what_depends_on_it(store):
    body = [{"id": "gate", "type": "condition", "needs": ["start"],
             "config": {"when": {"left": {"path": "loop.iteration"}, "op": "gte", "right": 2}}},
            {"id": "work", "type": "skill", "needs": ["gate"], "config": {"skill": "x.work"}}]
    skills = Skills()
    run_id, result = run(store, loop_flow(body, budget={"max_iterations": 3}, tail=False), skills)
    assert result["status"] == "completed"
    assert skills.iterations("work") == [2, 3]
    statuses = [r["result"]["body"]["work"]["status"] for r in store.iteration_rows(run_id, "lp")]
    assert statuses == ["skipped", "completed", "completed"]


def test_a_classify_in_the_body_routes_each_pass(store):
    from src.typed_decision import Decision
    body = [{"id": "route", "type": "classify", "needs": ["start"],
             "config": {"text": "pass {{ loop.iteration }}", "labels": ["easy", "hard"]}},
            {"id": "light", "type": "skill", "needs": ["route"], "branch": {"route": "easy"},
             "config": {"skill": "x.light"}},
            {"id": "heavy", "type": "skill", "needs": ["route"], "branch": {"route": "hard"},
             "config": {"skill": "x.heavy"}}]
    answers = iter(["easy", "hard"])

    class Models:
        def complete(self, *a, **k):
            raise AssertionError("not asked")

        def decide(self, context, fields, **k):
            value = next(answers)
            return {"label": Decision(field="label", value=value, confidence=0.9, mass=1.0,
                                      method="logprobs", best=value, model="m")}

    from src.workflows.model_calls import ModelCalls
    skills = Skills()
    handlers = default_handlers(skill=skills, models=ModelCalls(complete=Models().complete, decide=Models().decide))
    run_id = store.create_run(loop_flow(body, budget={"max_iterations": 2}, tail=False), owner="luis")["run_id"]
    assert WorkflowEngine(handlers, store).advance(run_id)["status"] == "completed"
    assert skills.iterations("light") == [1] and skills.iterations("heavy") == [2]


def test_body_nodes_honour_the_runs_permissions(store):
    skills = Skills()
    run_id, result = run(store, loop_flow(budget={"max_iterations": 2}, tail=False), skills,
                         permissions=["manual", "loop"])
    assert result["status"] == "failed" and skills.calls == []
    assert "policy_permission_denied" in store.node_runs(run_id)["lp"].reason


# ═════════════════════════ ceilings ═════════════════════════════════════

NEVER = {"left": {"path": "loop.results.step.n"}, "op": "gte", "right": 999}


def test_running_out_of_iterations_with_an_until_pauses_the_loop_by_default(store):
    skills = Skills()
    run_id, result = run(store, loop_flow(budget={"max_iterations": 2}, until=NEVER), skills)
    assert result["status"] == "paused" and result["waiting_on"] == "lp"
    node = store.node_runs(run_id)["lp"]
    assert node.status == "paused" and node.result["loop_budget_exhausted"] is True
    assert node.result["exhausted_by"] == "max_iterations" and "extend" in node.reason
    assert skills.iterations() == [1, 2] and skills.iterations("after") == []
    assert store.get_run(run_id)["run"].status == "paused"


def test_a_paused_exhausted_loop_can_be_extended_and_continues_from_where_it_stopped(store):
    skills = Skills()
    d = loop_flow(budget={"max_iterations": 2}, until={"left": {"path": "loop.results.step.n"}, "op": "gte", "right": 4})
    run_id, first = run(store, d, skills)
    assert first["status"] == "paused"
    engine = WorkflowEngine(default_handlers(skill=skills), store)
    out = engine.extend_loop(run_id, "lp", iterations=3, by="luis")
    assert out["ok"] is True and out["extended"]["iterations"] == 3
    assert out["advance"]["status"] == "completed"
    assert skills.iterations() == [1, 2, 3, 4], "iterations 1 and 2 are not run again"
    assert store.node_runs(run_id)["lp"].result["exited_by"] == "until"
    assert store.loop_extra(run_id, "lp")["iterations"] == 3


def test_resuming_without_extending_just_pauses_again(store):
    skills = Skills()
    run_id, _ = run(store, loop_flow(budget={"max_iterations": 2}, until=NEVER), skills)
    engine = WorkflowEngine(default_handlers(skill=skills), store)
    again = engine.resume(run_id, "lp")
    assert again["status"] == "paused" and skills.iterations() == [1, 2]


def test_on_exhausted_fail_ends_the_loop(store):
    skills = Skills()
    run_id, result = run(store, loop_flow(budget={"max_iterations": 2, "on_exhausted": "fail"}, until=NEVER), skills)
    assert result["status"] == "failed"
    assert "loop budget exhausted" in store.node_runs(run_id)["lp"].reason


@pytest.mark.parametrize("bad,reason", [
    ({"iterations": -1}, "whole numbers"), ({}, "nothing to add"),
    ({"iterations": 10_000}, "10000-iteration ceiling"), ({"iterations": True}, "whole numbers"),
])
def test_extending_is_validated(store, bad, reason):
    skills = Skills()
    run_id, _ = run(store, loop_flow(budget={"max_iterations": 2}, until=NEVER), skills)
    out = WorkflowEngine(default_handlers(skill=skills), store).extend_loop(run_id, "lp", **bad)
    assert out["ok"] is False and reason in out["reason"]
    assert store.loop_extra(run_id, "lp")["iterations"] == 0


def test_only_an_exhausted_loop_can_be_extended(store):
    skills = Skills()
    run_id, _ = run(store, loop_flow(budget={"max_iterations": 2}), skills)       # finished normally
    engine = WorkflowEngine(default_handlers(skill=skills), store)
    assert engine.extend_loop(run_id, "lp", iterations=1)["reason"] == "not_exhausted"
    assert engine.extend_loop(run_id, "after", iterations=1)["reason"] == "not_a_loop"
    assert engine.extend_loop("nope", "lp", iterations=1)["reason"] == "not_found"


def test_max_tool_calls_counts_effectful_body_nodes(store):
    skills = Skills()
    d = loop_flow(budget={"max_iterations": 10, "max_tool_calls": 2}, until=NEVER)
    run_id, result = run(store, d, skills)
    assert result["status"] == "paused" and skills.iterations() == [1, 2]
    node = store.node_runs(run_id)["lp"]
    assert node.result["exhausted_by"] == "max_tool_calls" and node.result["budget"]["used_tool_calls"] == 2


def test_max_seconds_counts_active_time_and_stops_a_slow_loop(store):
    def slow(node, context):
        time.sleep(0.5)
        return {"n": context["loop"]["iteration"]}

    skills = Skills(produce=slow)
    d = loop_flow(budget={"max_iterations": 10, "max_seconds": 1}, until=NEVER, tail=False)
    run_id, result = run(store, d, skills)
    assert result["status"] == "paused"
    assert store.node_runs(run_id)["lp"].result["exhausted_by"] == "max_seconds"
    assert 1 <= len(skills.calls) <= 3, "a second of active time is a few passes of half a second, not ten"


def test_loop_spend_is_part_of_the_runs_ledger(store):
    skills = Skills()
    run_id, _ = run(store, loop_flow(budget={"max_iterations": 3}, tail=False), skills)
    usage = store.usage_so_far(run_id)
    assert usage["tool_calls"] == 3


def test_the_runs_own_budget_stops_a_loop_between_passes(store, monkeypatch):
    from src import autonomy_budget
    monkeypatch.setattr(autonomy_budget, "resolve_budget",
                        lambda preset: autonomy_budget.Budget(max_tool_calls=2))
    skills = Skills()
    run_id, result = run(store, loop_flow(budget={"max_iterations": 9}, tail=False), skills)
    assert result["status"] == "paused"
    assert store.node_runs(run_id)["lp"].result["exhausted_by"] == "run_budget"
    assert skills.iterations() == [1, 2]


# ═════════════════════════ pauses inside a body ═════════════════════════

def test_a_body_node_that_pauses_pauses_the_loop_and_resumes_the_same_pass(store):
    state = {"asked": False}

    def produce(node, context):
        if node.id == "after":
            return {}
        if context["loop"]["iteration"] == 2 and not state["asked"]:
            state["asked"] = True
            return {"status": "paused", "approval_id": "apr_1", "reason": "needs a person", "partial": 7}
        return {"n": context["loop"]["iteration"], "previous_partial": (context["previous"] or {}).get("partial")}

    skills = Skills(produce=produce)
    run_id, first = run(store, loop_flow(budget={"max_iterations": 3}), skills)
    assert first["status"] == "paused" and first["approval_id"] == "apr_1" and first["waiting_on"] == "lp"
    assert skills.iterations() == [1, 2]
    engine = WorkflowEngine(default_handlers(skill=skills), store)
    done = engine.resume(run_id, "lp")
    assert done["status"] == "completed"
    assert skills.iterations() == [1, 2, 2, 3], "iteration 1 is not repeated; iteration 2 picks up where it paused"
    assert skills.calls[2]["previous"]["partial"] == 7
    assert store.node_runs(run_id)["lp"].attempt == 1, "coming back from a pause is not a new attempt"


def test_a_body_node_pausing_with_nothing_to_wait_for_fails_the_loop(store):
    skills = Skills(produce=lambda n, c: {"status": "paused"} if n.id == "step" else {})
    run_id, result = run(store, loop_flow(budget={"max_iterations": 2}), skills)
    assert result["status"] == "failed"
    assert "without an approval id or a wake time" in store.node_runs(run_id)["lp"].reason


# ═════════════════════════ restart ══════════════════════════════════════

def test_a_crash_between_passes_resumes_at_the_same_iteration(store):
    """Iteration 2 dies inside a node that only reads; the next engine picks up
    iteration 2, and iteration 1 is never run again."""
    body = [{"id": "read", "type": "extract", "needs": ["start"],
             "config": {"text": "x", "schema": {"type": "object"}}}]
    calls: List[int] = []

    def read(node, context):
        it = context["loop"]["iteration"]
        calls.append(it)
        if it == 2 and calls.count(2) == 1:
            raise SystemExit("killed")
        return {"data": {"i": it}}

    handlers = default_handlers()
    handlers["extract"] = read
    d = loop_flow(body, budget={"max_iterations": 3})
    run_id = store.create_run(d, owner="luis")["run_id"]
    with pytest.raises(SystemExit):
        WorkflowEngine(handlers, store).advance(run_id)
    assert calls == [1, 2]
    rows = store.iteration_rows(run_id, "lp")
    assert [(r["iteration"], r["status"]) for r in rows] == [(1, "completed"), (2, "running")]

    recovered = store.recover_expired_node_leases(now=FAR_FUTURE)
    assert [(h["node_id"], h["outcome"]) for h in recovered if h["node_id"] == "lp"] == [("lp", "released")]
    skills = Skills()
    handlers2 = default_handlers(skill=skills)
    handlers2["extract"] = read
    result = WorkflowEngine(handlers2, store).advance(run_id)
    assert result["status"] == "completed"
    assert calls == [1, 2, 2, 3], "iteration 1 was not repeated; 2 was retried once; 3 ran"
    assert store.node_runs(run_id)["lp"].result["iterations"] == 3


def test_a_crash_inside_an_effectful_body_node_is_an_unknown_effect_and_is_not_repeated(store):
    skills = Skills(crash_on=("step", 2))
    d = loop_flow(budget={"max_iterations": 3})
    run_id = store.create_run(d, owner="luis")["run_id"]
    with pytest.raises(SystemExit):
        WorkflowEngine(default_handlers(skill=skills), store).advance(run_id)
    assert skills.iterations() == [1, 2]
    store.recover_expired_node_leases(now=FAR_FUTURE)
    result = WorkflowEngine(default_handlers(skill=skills), store).advance(run_id)
    assert result["status"] == "failed"
    reason = store.node_runs(run_id)["lp"].reason
    assert "unknown_effect" in reason and "iteration 2" in reason and "step" in reason
    assert skills.iterations() == [1, 2], "the effect that may have landed is never repeated to find out"
    assert skills.iterations("after") == []
    body = store.iteration_rows(run_id, "lp")[1]["result"]["body"]["step"]
    assert body["status"] == "failed" and body["effect"] == "unknown"


def test_a_finished_loop_is_not_run_again_by_a_second_engine(store):
    skills = Skills()
    run_id, _ = run(store, loop_flow(budget={"max_iterations": 2}), skills)
    before = list(skills.calls)
    again = WorkflowEngine(default_handlers(skill=skills), store).advance(run_id)
    assert again["reason"] == "already_completed" and skills.calls == before


def test_a_crash_after_the_last_pass_is_written_but_before_the_loop_finishes_does_not_rerun_passes(store):
    """Every pass row is complete, the loop node's own result was never
    written: the resumed loop reads the rows and finishes without running a
    body node."""
    skills = Skills()
    d = loop_flow(budget={"max_iterations": 2}, tail=False)
    run_id = store.create_run(d, owner="luis")["run_id"]
    from src.workflows import store as store_mod
    killed = {"n": 0}

    def hook(point, **ctx):
        if point == "before_result" and ctx.get("node_id") == "lp" and ctx.get("status") == "completed" and not killed["n"]:
            killed["n"] = 1
            raise SystemExit("killed before the loop result was written")

    previous = store_mod.set_fault_hook(hook)
    try:
        with pytest.raises(SystemExit):
            WorkflowEngine(default_handlers(skill=skills), store).advance(run_id)
    finally:
        store_mod.set_fault_hook(previous)
    assert skills.iterations() == [1, 2]
    store.recover_expired_node_leases(now=FAR_FUTURE)
    result = WorkflowEngine(default_handlers(skill=skills), store).advance(run_id)
    assert result["status"] == "completed" and skills.iterations() == [1, 2]
    assert store.node_runs(run_id)["lp"].result["iterations"] == 2


def test_a_crash_after_until_held_finishes_without_another_pass(store):
    until = {"left": {"path": "loop.results.step.n"}, "op": "gte", "right": 1}
    skills = Skills()
    d = loop_flow(budget={"max_iterations": 5}, until=until, tail=False)
    run_id = store.create_run(d, owner="luis")["run_id"]
    from src.workflows import store as store_mod

    def hook(point, **ctx):
        if point == "before_result" and ctx.get("node_id") == "lp" and ctx.get("status") == "completed":
            hook.armed = getattr(hook, "armed", 0) + 1
            if hook.armed == 1:
                raise SystemExit("killed")

    previous = store_mod.set_fault_hook(hook)
    try:
        with pytest.raises(SystemExit):
            WorkflowEngine(default_handlers(skill=skills), store).advance(run_id)
    finally:
        store_mod.set_fault_hook(previous)
    store.recover_expired_node_leases(now=FAR_FUTURE)
    WorkflowEngine(default_handlers(skill=skills), store).advance(run_id)
    assert skills.iterations() == [1]
    assert store.node_runs(run_id)["lp"].result["exited_by"] == "until"


def test_a_cancelled_run_stops_the_loop_before_the_next_body_node(store):
    holder: Dict[str, Any] = {}

    def produce(node, context):
        if context["loop"]["iteration"] == 1:
            store.set_run_status(holder["run_id"], "cancelled", reason="stopped by a person")
        return {"n": context["loop"]["iteration"]}

    skills = Skills(produce=produce)
    d = loop_flow(budget={"max_iterations": 5}, tail=False)
    run_id = store.create_run(d, owner="luis")["run_id"]
    holder["run_id"] = run_id
    result = WorkflowEngine(default_handlers(skill=skills), store).advance(run_id)
    assert result["status"] == "cancelled"
    assert skills.iterations() == [1]


# ═════════════════════════ retrying a loop ══════════════════════════════

def test_retrying_a_finished_loop_with_a_reading_body_starts_it_over(store):
    body = [{"id": "read", "type": "extract", "needs": ["start"],
             "config": {"text": "x", "schema": {"type": "object"}}}]
    calls: List[int] = []
    handlers = default_handlers()
    handlers["extract"] = lambda node, ctx: (calls.append(ctx["loop"]["iteration"]) or {"data": {}})
    d = loop_flow(body, budget={"max_iterations": 2}, tail=False)
    run_id = store.create_run(d, owner="luis")["run_id"]
    engine = WorkflowEngine(handlers, store)
    engine.advance(run_id)
    assert store.retry_node(run_id, "lp", d)["ok"] is True
    assert store.iteration_rows(run_id, "lp") == []
    engine.advance(run_id)
    assert calls == [1, 2, 1, 2]


def test_a_loop_whose_body_reaches_outside_cannot_be_replayed(store):
    skills = Skills()
    d = loop_flow(budget={"max_iterations": 2}, tail=False)
    run_id, _ = run(store, d, skills)
    out = store.retry_node(run_id, "lp", d)
    assert out["ok"] is False and "reaches outside" in out["reason"]
    assert len(store.iteration_rows(run_id, "lp")) == 2


def test_opening_an_iteration_twice_returns_the_same_row(store):
    d = loop_flow()
    run_id = store.create_run(d, owner="luis")["run_id"]
    first = store.open_iteration(run_id, "lp", 1, key="k1")
    second = store.open_iteration(run_id, "lp", 1, key="k1")
    assert first["opened"] is True and second["opened"] is False
    assert len(store.iteration_rows(run_id, "lp")) == 1


# ═════════════════════════ tools that read a definition ═════════════════

def test_the_linter_knows_a_loop_is_bounded_and_asks_for_the_right_things():
    from src.agent_profile_lint import lint_workflow
    body = [{"id": "send", "type": "deliver", "needs": ["start"], "config": {"to": "x"}}]
    findings = {f.code for f in lint_workflow(loop_flow(body, budget={"max_iterations": 3}, tail=False))}
    assert "LINT-WF-CYCLE-NO-BOUND" not in findings
    assert "LINT-WF-LOOP-NO-UNTIL" in findings and "LINT-WF-LOOP-EFFECT-UNMETERED" in findings
    until = {"left": {"path": "loop.iteration"}, "op": "gte", "right": 2}
    bounded = {f.code for f in lint_workflow(loop_flow(
        body, budget={"max_iterations": 3, "max_tool_calls": 3}, until=until, tail=False))}
    assert "LINT-WF-LOOP-NO-UNTIL" not in bounded and "LINT-WF-LOOP-EFFECT-UNMETERED" not in bounded
    assert "LINT-WF-OUTPUT-NO-EVALUATOR" not in bounded, "an `until` evaluates what the body produces"


def test_the_estimate_multiplies_a_loop_body_by_its_iteration_range():
    from src.workflow_cost_estimate import estimate, estimate_detailed
    body = [{"id": "think", "type": "agent", "needs": ["start"],
             "config": {"prompt": "x", "max_rounds": 4}}]
    fixed = loop_flow(body, budget={"max_iterations": 5}, tail=False)
    rows = {r["node_id"]: r for r in estimate(fixed).per_node}
    assert (rows["think"]["calls_min"], rows["think"]["calls_max"]) == (5, 5)
    assert (rows["think"]["model_calls_min"], rows["think"]["model_calls_max"]) == (5, 20)
    assert rows["think"]["iterations"] == [5, 5] and estimate(fixed).unbounded_loops == ()
    early = loop_flow(body, budget={"max_iterations": 5}, until={"left": {"path": "loop.iteration"}, "op": "gte", "right": 2}, tail=False)
    rows = {r["node_id"]: r for r in estimate(early).per_node}
    assert (rows["think"]["calls_min"], rows["think"]["calls_max"]) == (1, 5)
    detail = estimate_detailed(early).to_dict()
    think = next(r for r in detail["per_node"] if r["node_id"] == "think")
    assert think["model_calls"] == {"min": 1, "max": 20} and think["structural_bounds"] == {"min": 1, "max": 5}
    assert detail["structural_bounds"]["node_activations"]["max"] != "unbounded"


def test_a_branch_gated_node_might_not_run_in_the_estimate():
    from src.workflow_cost_estimate import estimate
    d = parse([START,
               {"id": "g", "type": "guard", "needs": ["start"], "config": {"text": "x", "checks": ["secrets"]}},
               {"id": "ok", "type": "skill", "needs": ["g"], "branch": {"g": "pass"}, "config": {"skill": "x.y"}}])
    rows = {r["node_id"]: r for r in estimate(d).per_node}
    assert (rows["ok"]["calls_min"], rows["ok"]["calls_max"]) == (0, 1)


def test_preflight_counts_a_loops_model_calls_and_says_the_model_is_unknown():
    from src.workflows.preflight import preflight
    body = [{"id": "think", "type": "agent", "needs": ["start"],
             "config": {"prompt": "{{ inputs.topic }}", "tools": ["read_file"], "max_rounds": 2}}]
    report = preflight(loop_flow(body, budget={"max_iterations": 3}, tail=False)).to_dict()
    row = next(r for r in report["token_estimate"]["per_node"] if r["node_id"] == "think")
    assert row["tokens_max"] == 3 * 2 * 2000 and row["iterations"] == [3, 3]
    assert report["cost"]["scenario"] == "unknown"
    assert "read_file" in report["tools"] and "inputs.topic" in report["inputs"]


def test_a_simulation_reports_a_loop_as_a_unit_and_does_not_walk_its_body():
    from src.workflows.simulate import simulate
    sim = simulate(loop_flow(budget={"max_iterations": 4}, until=NEVER))
    assert "step" not in sim.activated and {"lp", "after"} <= set(sim.activated)
    assert sim.loops["lp"] == {"body": ["step"], "max_iterations": 4, "min_iterations": 1,
                               "has_until": True, "on_exhausted": "pause"}
    assert any("loop 'lp' runs its body" in w for w in sim.warnings)
    assert sim.to_dict()["loops"]["lp"]["max_iterations"] == 4


def test_the_mermaid_export_draws_the_loop_and_its_body():
    from src.topology_export import workflow_to_mermaid
    text = workflow_to_mermaid(loop_flow(budget={"max_iterations": 4}))
    assert 'lp -.->|"repeats up to 4x"| step' in text and "nt_loop" in text


# ═════════════════════════ over HTTP ════════════════════════════════════

@pytest.fixture()
def client(store, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core import middleware
    import routes.workflows_routes as wr
    monkeypatch.setattr(middleware, "auth_disabled", lambda: True)
    skills = Skills()
    monkeypatch.setattr(wr, "_engine", lambda s: WorkflowEngine(default_handlers(skill=skills), s))
    app = FastAPI()
    app.include_router(wr.setup_workflows_routes())
    c = TestClient(app)
    c.skills = skills
    return c


def test_the_run_view_lists_each_pass_and_an_exhausted_loop_can_be_extended_over_http(client):
    d = loop_flow(budget={"max_iterations": 2},
                  until={"left": {"path": "loop.results.step.n"}, "op": "gte", "right": 3}).to_dict()
    created = client.post("/api/workflows/runs", json={"definition": d, "advance": True}).json()
    run_id = created["run_id"]
    assert created["result"]["status"] == "paused"
    view = client.get(f"/api/workflows/runs/{run_id}").json()
    assert [p["iteration"] for p in view["loops"]["lp"]] == [1, 2]
    assert view["loops"]["lp"][0]["nodes"]["step"]["status"] == "completed"
    assert view["nodes"]["lp"]["result"]["loop_budget_exhausted"] is True

    assert client.post(f"/api/workflows/runs/{run_id}/nodes/lp/extend", json={"bogus": 1}).status_code == 400
    assert client.post(f"/api/workflows/runs/{run_id}/nodes/lp/extend", json={}).status_code == 409
    assert client.post(f"/api/workflows/runs/{run_id}/nodes/after/extend", json={"iterations": 1}).status_code == 409
    assert client.post("/api/workflows/runs/nope/nodes/lp/extend", json={"iterations": 1}).status_code == 404
    ok = client.post(f"/api/workflows/runs/{run_id}/nodes/lp/extend", json={"iterations": 2})
    assert ok.status_code == 200 and ok.json()["advance"]["status"] == "completed"
    final = client.get(f"/api/workflows/runs/{run_id}").json()
    assert final["run"]["status"] == "completed" and len(final["loops"]["lp"]) == 3


def test_validate_refuses_an_unbounded_loop_and_names_the_field(client):
    bad = loop_flow().to_dict()
    bad["nodes"][1]["config"]["budget"] = {}
    out = client.post("/api/workflows/validate", json={"definition": bad}).json()
    assert out["ok"] is False and "max_iterations" in out["field"]
    started = client.post("/api/workflows/runs", json={"definition": bad})
    assert started.status_code == 400 and "max_iterations" in started.json()["detail"]
