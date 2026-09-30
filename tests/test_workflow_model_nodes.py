"""The workflow nodes that use a model: `agent`, `classify`, `extract`, `guard`.

What is pinned here, beyond "the handler returns something":

* every one runs through the real engine and the real store, so the claim,
  the idempotency key and the recorded result are the production ones;
* a node with nothing wired refuses by name instead of pretending;
* a model's structured answer is checked, repaired once, then failed;
* an `agent` turn — which may call tools — is never run twice after a crash.

The model is always a fake: a function that records what it was asked and
answers from a script. Nothing in this file reaches a network.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database as db_mod
from core.database import Base
from src.contracts import ContractError, WorkflowDefinition
from src.workflows import WorkflowEngine, WorkflowStore, default_handlers
from src.workflows.model_calls import ModelCalls, ModelUnavailable


@pytest.fixture()
def store(tmp_path, monkeypatch):
    url = "sqlite:///" + (tmp_path / "wf_model.db").as_posix()
    engine = create_engine(url, connect_args={"check_same_thread": False})
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal",
                        sessionmaker(autocommit=False, autoflush=False, bind=engine))
    Base.metadata.create_all(bind=engine)
    yield WorkflowStore()
    engine.dispose()


def flow(*nodes: Dict[str, Any], wid: str = "model.flow") -> WorkflowDefinition:
    return WorkflowDefinition.parse({"id": wid, "version": "1.0.0", "title": "Model flow",
                                     "nodes": list(nodes)})


START = {"id": "start", "type": "manual"}


class FakeModels:
    """Scripted `ModelCalls`: `completions` are returned in order, `decisions`
    maps a field name to the Decision-like object `decide` should answer."""

    def __init__(self, completions: List[str] = (), decisions: Dict[str, Any] = None):
        self.completions = list(completions)
        self.decisions = dict(decisions or {})
        self.complete_calls: List[Dict[str, Any]] = []
        self.decide_calls: List[Dict[str, Any]] = []

    def complete(self, messages, **opts):
        self.complete_calls.append({"messages": messages, **opts})
        if not self.completions:
            raise ModelUnavailable("the script ran out of answers")
        answer = self.completions.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def decide(self, context, fields, **opts):
        self.decide_calls.append({"context": context, "fields": list(fields), **opts})
        return {f.name: self.decisions[f.name] for f in fields if f.name in self.decisions}

    def as_calls(self) -> ModelCalls:
        return ModelCalls(complete=self.complete, decide=self.decide)


def run_flow(store, definition, handlers, inputs=None, **create):
    run_id = store.create_run(definition, owner="luis", inputs=inputs or {}, **create)["run_id"]
    result = WorkflowEngine(handlers, store).advance(run_id)
    return run_id, result


# ═════════════════════════ agent ═════════════════════════════════════════

def agent_flow(**config):
    cfg = {"prompt": "Summarise: {{ inputs.ticket }}", "tools": ["read_file"]}
    cfg.update(config)
    return flow(START, {"id": "think", "type": "agent", "needs": ["start"], "config": cfg})


class FakeAgent:
    def __init__(self, text="the summary", **extra):
        self.calls: List[Dict[str, Any]] = []
        self.text = text
        self.extra = extra

    def __call__(self, spec):
        self.calls.append(dict(spec))
        begin = spec.get("begin_effect")
        if callable(begin):
            begin()
        return {"text": self.text, "rounds": 2, "tool_calls": 1, "model": "local-27b",
                "profile": spec.get("agent", ""), "session_id": "wf-test",
                "tool_events": [{"round": 1, "tool": "read_file", "exit_code": 0}],
                **self.extra}


def test_an_agent_node_runs_one_turn_with_a_rendered_prompt_and_records_the_answer(store):
    agent = FakeAgent()
    run_id, result = run_flow(store, agent_flow(agent="reviewer", max_rounds=5, timeout_s=60),
                              default_handlers(agent=agent), inputs={"ticket": "printer on fire"})
    assert result["status"] == "completed"
    assert len(agent.calls) == 1
    spec = agent.calls[0]
    assert spec["prompt"] == "Summarise: printer on fire"
    assert spec["tools"] == ["read_file"] and spec["agent"] == "reviewer"
    assert spec["max_rounds"] == 5 and spec["timeout_s"] == 60
    assert spec["owner"] == "luis" and spec["node_id"] == "think"
    out = store.node_runs(run_id)["think"]
    assert out.status == "completed"
    assert out.result["text"] == "the summary"
    assert out.result["model"] == "local-27b" and out.result["tool_calls"] == 1
    # The node carries the same idempotency key the store claimed it under.
    assert out.result["idempotency_key"] == out.idempotency_key != ""


def test_an_agent_prompt_can_read_an_upstream_nodes_output(store):
    first = FakeAgent(text="draft v1")
    second = FakeAgent(text="final")
    d = flow(START,
             {"id": "draft", "type": "agent", "needs": ["start"], "config": {"prompt": "write it"}},
             {"id": "polish", "type": "agent", "needs": ["draft"],
              "config": {"prompt": "improve: {{ results.draft.text }}", "tools": []}})
    calls = {"n": 0}

    def runner(spec):
        calls["n"] += 1
        return (first if calls["n"] == 1 else second)(spec)

    run_id, result = run_flow(store, d, default_handlers(agent=runner))
    assert result["status"] == "completed"
    assert second.calls[0]["prompt"] == "improve: draft v1"


def test_an_agent_with_no_runner_refuses_by_name(store):
    run_id, result = run_flow(store, agent_flow(), default_handlers(), inputs={"ticket": "x"})
    assert result["status"] == "failed"
    node = store.node_runs(run_id)["think"]
    assert "no agent runner is wired" in node.reason and "nothing ran" in node.reason


def test_a_prompt_that_names_something_the_run_never_produced_fails_before_the_turn(store):
    agent = FakeAgent()
    run_id, result = run_flow(store, agent_flow(prompt="use {{ results.ghost.text }}"),
                              default_handlers(agent=agent))
    assert result["status"] == "failed" and agent.calls == []
    assert "results.ghost.text" in store.node_runs(run_id)["think"].reason


@pytest.mark.parametrize("bad,needle", [
    ({"prompt": ""}, "config.prompt"),
    ({"timeout_s": 0}, "timeout_s"),
    ({"timeout_s": "soon"}, "timeout_s"),
    ({"max_rounds": 0}, "max_rounds"),
    ({"max_rounds": 500}, "max_rounds"),
    ({"tools": "read_file"}, "config.tools"),
    ({"output_schema": {"$ref": "#/x"}}, "cannot be enforced"),
])
def test_a_badly_configured_agent_fails_with_the_field_named(store, bad, needle):
    agent = FakeAgent()
    cfg = {"prompt": "hi", "tools": ["read_file"]}
    cfg.update(bad)
    if bad.get("prompt") == "":
        cfg["prompt"] = ""
    run_id, result = run_flow(store, flow(START, {"id": "think", "type": "agent",
                                                   "needs": ["start"], "config": cfg}),
                              default_handlers(agent=agent))
    assert result["status"] == "failed" and agent.calls == []
    assert needle in store.node_runs(run_id)["think"].reason


def test_an_agent_answer_is_checked_against_its_output_schema(store):
    schema = {"type": "object", "required": ["verdict"],
              "properties": {"verdict": {"enum": ["ok", "bad"]}}}
    agent = FakeAgent(text='Here you go:\n```json\n{"verdict": "ok"}\n```')
    run_id, result = run_flow(store, agent_flow(output_schema=schema), default_handlers(agent=agent),
                              inputs={"ticket": "t"})
    assert result["status"] == "completed"
    out = store.node_runs(run_id)["think"].result
    assert out["data"] == {"verdict": "ok"} and out["repaired"] is False


def test_an_invalid_answer_gets_exactly_one_repair_and_then_passes(store):
    schema = {"type": "object", "required": ["verdict"],
              "properties": {"verdict": {"enum": ["ok", "bad"]}}}
    agent = FakeAgent(text='{"verdict": "maybe"}')
    models = FakeModels(completions=['{"verdict": "ok"}'])
    run_id, result = run_flow(store, agent_flow(output_schema=schema),
                              default_handlers(agent=agent, models=models.as_calls()),
                              inputs={"ticket": "t"})
    assert result["status"] == "completed"
    out = store.node_runs(run_id)["think"].result
    assert out["data"] == {"verdict": "ok"} and out["repaired"] is True
    assert len(models.complete_calls) == 1
    sent = json.dumps(models.complete_calls[0]["messages"])
    assert "verdict" in sent and "must be one of" in sent        # the exact problem was shown
    assert len(agent.calls) == 1, "the repair must not run a second agent turn"


def test_a_failed_repair_fails_the_node_and_the_turn_is_not_retried(store):
    schema = {"type": "object", "required": ["verdict"]}
    agent = FakeAgent(text="no json at all")
    models = FakeModels(completions=["still not json"])
    d = flow(START, {"id": "think", "type": "agent", "needs": ["start"], "max_attempts": 3,
                     "config": {"prompt": "go", "tools": [], "output_schema": schema}})
    run_id, result = run_flow(store, d, default_handlers(agent=agent, models=models.as_calls()))
    assert result["status"] == "failed"
    node = store.node_runs(run_id)["think"]
    assert "output_schema" in node.reason and "tried once" in node.reason
    assert len(models.complete_calls) == 1
    assert len(agent.calls) == 1, "a confirmed turn must not be retried automatically"


def test_a_timed_out_turn_fails_and_leaves_the_effect_unknown(store):
    agent = FakeAgent(text="half", stop_reason="timeout")
    run_id, result = run_flow(store, agent_flow(), default_handlers(agent=agent),
                              inputs={"ticket": "t"})
    assert result["status"] == "failed"
    node = store.node_runs(run_id)["think"]
    assert "did not finish" in node.reason
    assert store.effect_state(run_id, "think", 1) == "unknown"
    assert store.needs_reconciliation(run_id=run_id)[0]["node_id"] == "think"


def test_an_agent_that_asks_for_an_approval_fails_with_the_way_out(store):
    agent = FakeAgent(text="", approvals_requested=1)
    run_id, result = run_flow(store, agent_flow(), default_handlers(agent=agent),
                              inputs={"ticket": "t"})
    assert result["status"] == "failed"
    assert "human_approval" in store.node_runs(run_id)["think"].reason


def test_a_turn_that_could_not_start_is_a_failure_without_an_effect(store):
    class Unavailable(RuntimeError):
        pass
    Unavailable.__name__ = "AgentTurnUnavailable"

    def runner(spec):
        raise Unavailable("no model endpoint is configured for dispatch")

    run_id, result = run_flow(store, agent_flow(), default_handlers(agent=runner),
                              inputs={"ticket": "t"})
    assert result["status"] == "failed"
    assert "could not start" in store.node_runs(run_id)["think"].reason
    assert store.effect_state(run_id, "think", 1) == "none"


def test_a_crash_mid_turn_never_runs_the_turn_twice_after_restart(store):
    """The phase's stop condition, for an agent: the process dies after the
    turn acted and before its result was written. Whoever picks the run up
    finds an unknown effect and a reconciliation entry — not a second turn."""
    turns = []

    def dying(spec):
        spec["begin_effect"]()
        turns.append("turn")
        raise SystemExit("power cut")

    d = agent_flow()
    run_id = store.create_run(d, owner="luis", inputs={"ticket": "t"})["run_id"]
    with pytest.raises(SystemExit):
        WorkflowEngine(default_handlers(agent=dying), store).advance(run_id)
    assert turns == ["turn"]

    store.recover_expired_node_leases(now="2999-01-01T00:00:00Z")
    again = FakeAgent()
    result = WorkflowEngine(default_handlers(agent=again), WorkflowStore()).advance(run_id)
    assert again.calls == [], "the restart ran the agent turn again"
    assert result["status"] == "failed"
    node = store.node_runs(run_id)["think"]
    assert "unknown_effect" in node.reason
    assert store.needs_reconciliation(run_id=run_id)


def test_a_finished_agent_node_is_not_run_again_by_a_second_engine(store):
    agent = FakeAgent()
    run_id, _ = run_flow(store, agent_flow(), default_handlers(agent=agent), inputs={"ticket": "t"})
    again = FakeAgent()
    result = WorkflowEngine(default_handlers(agent=again), WorkflowStore()).advance(run_id)
    assert result["reason"] == "already_completed" and again.calls == []


def test_retrying_an_agent_that_can_use_tools_needs_the_authors_word():
    with pytest.raises(ContractError) as err:
        flow({"id": "a", "type": "agent", "max_attempts": 2, "config": {"prompt": "x"}})
    assert "idempotent" in err.value.message
    flow({"id": "a", "type": "agent", "max_attempts": 2,
          "config": {"prompt": "x", "idempotent": True}})
    # Thinking only is free to repeat: no tools, nothing to duplicate.
    flow({"id": "a", "type": "agent", "max_attempts": 2, "config": {"prompt": "x", "tools": []}})


# ── the production runner, with the agent loop replaced by a scripted stream ──

def test_the_production_agent_runner_drives_the_one_agent_loop(monkeypatch):
    from src.workflows import agent_turn

    seen: Dict[str, Any] = {}

    async def fake_loop(url, model, messages, **kwargs):
        seen.update(url=url, model=model, messages=messages, **kwargs)
        yield 'data: {"type": "agent_step", "round": 1}\n\n'
        yield 'data: {"type": "tool_output", "tool": "read_file", "exit_code": 0}\n\n'
        yield 'data: {"delta": "all "}\n\n'
        yield 'data: {"delta": "done"}\n\n'
        yield "data: [DONE]\n\n"

    import src.agent_loop as agent_loop
    from src import dispatch
    monkeypatch.setattr(agent_loop, "stream_agent_loop", fake_loop)
    monkeypatch.setattr(dispatch, "resolve_route",
                        lambda owner, model=None: ("http://127.0.0.1:8080/v1", model or "local-27b", {"x": "y"}))
    started = []
    out = agent_turn.run({"prompt": "do it", "system": "be brief", "tools": ["read_file"],
                          "max_rounds": 3, "timeout_s": 30, "owner": "luis",
                          "run_id": "wfr_1", "node_id": "think",
                          "begin_effect": lambda: started.append(True) or True})
    assert out["text"] == "all done" and out["rounds"] == 1 and out["tool_calls"] == 1
    assert out["model"] == "local-27b" and started == [True]
    assert seen["max_rounds"] == 3 and seen["owner"] == "luis"
    assert seen["messages"][0] == {"role": "system", "content": "be brief"}
    assert seen["messages"][-1] == {"role": "user", "content": "do it"}
    # Only the listed tool survives; delegation is never on.
    assert "delegate_agents" in seen["disabled_tools"]
    assert "read_file" not in seen["disabled_tools"]
    assert "web_search" in seen["disabled_tools"] or "bash" in seen["disabled_tools"]


def test_the_production_runner_stops_at_its_deadline(monkeypatch):
    from src.workflows import agent_turn

    async def slow_loop(*a, **k):
        yield 'data: {"delta": "partial"}\n\n'
        await asyncio.sleep(60)
        yield 'data: {"delta": "never"}\n\n'

    import src.agent_loop as agent_loop
    from src import dispatch
    monkeypatch.setattr(agent_loop, "stream_agent_loop", slow_loop)
    monkeypatch.setattr(dispatch, "resolve_route", lambda owner, model=None: ("http://x/v1", "m", {}))
    monkeypatch.setattr(agent_turn, "DEFAULT_TIMEOUT_S", 5)
    # The floor is 5s; shrink the wait inside the consumer instead of sleeping for it.
    real_consume = agent_turn._consume

    async def quick(stream, *, deadline, cancelled):
        import time
        return await real_consume(stream, deadline=min(deadline, time.monotonic() + 0.2),
                                  cancelled=cancelled)

    monkeypatch.setattr(agent_turn, "_consume", quick)
    out = agent_turn.run({"prompt": "x", "timeout_s": 5, "owner": "luis"})
    assert out["stop_reason"] == "timeout" and out["text"] == "partial"


def test_the_production_runner_refuses_before_starting_when_no_model_is_configured(monkeypatch):
    from src.workflows import agent_turn
    from src import dispatch

    def no_route(owner, model=None):
        raise ValueError("no model endpoint is configured for dispatch")

    monkeypatch.setattr(dispatch, "resolve_route", no_route)
    started = []
    with pytest.raises(agent_turn.AgentTurnUnavailable):
        agent_turn.run({"prompt": "x", "begin_effect": lambda: started.append(1) or True})
    assert started == [], "an effect was claimed for a turn that never began"
