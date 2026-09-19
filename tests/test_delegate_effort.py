"""delegate_agents `effort` (low/medium/high): schema, the pure mapping in
src/effort_profile.py, and that a child run actually receives the right
request options (src/agent_tools/subagent_tools.py -> src/llm_core.py).

Follows the existing patterns: tests/test_objective_tool_schema.py for the
ast.literal_eval schema check, tests/test_subagents_v2.py for driving
DelegateAgentsTool with a stream_agent_loop stand-in, and
tests/test_llm_core_temperature_reasoning.py for the payload-capturing
_FakeClient at the llm_core level.
"""
from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path

import pytest

from src import effort_profile
from src.agent_tools import subagent_tools as st


# ── 1. schema ────────────────────────────────────────────────────────────

def _delegate_agents_task_schema():
    source = Path(__file__).resolve().parent.parent / "src" / "tool_schemas.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    assignment = next(node for node in tree.body if isinstance(node, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == "FUNCTION_TOOL_SCHEMAS"
                              for t in node.targets))
    schema = next(s["function"] for s in ast.literal_eval(assignment.value)
                  if s["function"]["name"] == "delegate_agents")
    return schema["parameters"]["properties"]["tasks"]["items"]


def test_schema_declares_optional_effort_with_three_levels():
    task_item = _delegate_agents_task_schema()
    effort = task_item["properties"]["effort"]
    assert effort["type"] == "string"
    assert effort["enum"] == ["low", "medium", "high"]
    assert isinstance(effort.get("description"), str) and effort["description"]
    # Optional: `instruction` alone is still required, effort is not.
    assert "effort" not in task_item["required"]


def test_schema_task_omitting_effort_is_still_valid_shape():
    task_item = _delegate_agents_task_schema()
    assert task_item["required"] == ["instruction"]


# ── 2. effort_profile.resolve() ─────────────────────────────────────────

@pytest.mark.parametrize("effort", [None, "", "medium", "MEDIUM", "bogus", "  "])
def test_resolve_inherits_current_behaviour(effort):
    result = effort_profile.resolve(effort)
    assert result == {"gen_overrides": None, "hint": ""}


def test_resolve_low_disables_thinking_and_is_brief():
    result = effort_profile.resolve("low", model="qwen3.8-27b-q8-llamacpp", endpoint="http://127.0.0.1:8081/v1")
    assert result["gen_overrides"]["think"] is False
    assert result["gen_overrides"]["reasoning_effort"] == "low"
    assert result["gen_overrides"]["reasoning_budget"] == 0
    assert "brief" in result["hint"].lower()


def test_resolve_high_enables_thinking_with_a_larger_budget():
    result = effort_profile.resolve("HIGH", model="qwen3.8-27b-q8-llamacpp", endpoint="http://127.0.0.1:8081/v1")
    assert result["gen_overrides"]["think"] is True
    assert result["gen_overrides"]["reasoning_effort"] == "high"
    assert result["gen_overrides"]["reasoning_budget"] > effort_profile.DEFAULT_REASONING_BUDGET
    assert "verify" in result["hint"].lower() or "carefully" in result["hint"].lower()


@pytest.mark.parametrize("endpoint", [
    "http://127.0.0.1:11434/v1",          # Ollama
    "http://127.0.0.1:8081/v1",           # self-hosted llama.cpp/vLLM
    "https://api.openai.com/v1",          # remote, no thinking controls at all
])
def test_resolve_mapping_is_stable_across_backend_kinds(endpoint):
    # resolve() itself does not special-case a backend: llm_core.py is the
    # layer that drops a knob a given backend does not understand, so a
    # backend "without thinking controls" gets the exact same gen_overrides
    # back (inert there) plus the hint, which is the documented fallback.
    low = effort_profile.resolve("low", model="some-model", endpoint=endpoint)
    high = effort_profile.resolve("high", model="some-model", endpoint=endpoint)
    assert low["hint"] and high["hint"]
    assert low["gen_overrides"]["think"] is False
    assert high["gen_overrides"]["think"] is True


def test_merge_gen_overrides_leaves_base_alone_when_nothing_to_merge():
    base = {"top_p": 0.9}
    assert effort_profile.merge_gen_overrides(base, None) is base
    assert effort_profile.merge_gen_overrides(base, {}) is base


def test_merge_gen_overrides_layers_effort_over_the_caller_base():
    base = {"top_p": 0.9, "think": True}
    merged = effort_profile.merge_gen_overrides(base, {"think": False, "reasoning_effort": "low"})
    assert merged == {"top_p": 0.9, "think": False, "reasoning_effort": "low"}
    assert base == {"top_p": 0.9, "think": True}  # base itself untouched


# ── 3. parse_delegation_args carries `effort` per task ──────────────────

def test_parse_delegation_args_accepts_effort_per_task():
    args = st.parse_delegation_args(json.dumps({"tasks": [
        {"instruction": "rename foo to bar", "effort": "low"},
        {"instruction": "design the schema", "effort": "high"},
        {"instruction": "normal work"},
    ]}))
    t0, t1, t2 = args["tasks"]
    assert t0["effort"] == "low"
    assert t1["effort"] == "high"
    assert "effort" not in t2  # omitted -> byte-for-byte the old shape


def test_parse_delegation_args_drops_unrecognised_effort():
    args = st.parse_delegation_args(json.dumps({"tasks": [
        {"instruction": "x", "effort": "extreme"},
    ]}))
    assert "effort" not in args["tasks"][0]


def test_parse_delegation_args_normalizes_explicit_medium():
    # "medium" is a recognised level (kept on the row, shown on the card) even
    # though effort_profile.resolve("medium") is itself a no-op — surfacing
    # what was asked for is not the same thing as changing the request.
    args = st.parse_delegation_args(json.dumps({"tasks": [{"instruction": "y", "effort": "MEDIUM"}]}))
    assert args["tasks"][0]["effort"] == "medium"


def test_subagent_run_normalizes_effort():
    run_low = st.SubagentRun(0, {"name": "a", "instruction": "x", "effort": "low"})
    run_none = st.SubagentRun(1, {"name": "b", "instruction": "y"})
    run_bad = st.SubagentRun(2, {"name": "c", "instruction": "z", "effort": "nope"})
    assert run_low.effort == "low"
    assert run_none.effort == ""
    assert run_bad.effort == ""


# ── 4. the child run actually receives the right request options ───────

class _SM:
    def __init__(self):
        self.sessions = {}

    def create_session(self, session_id, **kw):
        self.sessions[session_id] = type(
            "S", (), {"messages": [], "add_message": lambda self, m: self.messages.append(m)}
        )()

    def get_session(self, sid):
        return self.sessions.get(sid)

    def save_sessions(self):
        pass


def _harness_summary(mutations):
    return "data: " + json.dumps({"type": "harness_summary", "data": {"mutations": mutations, "stop_reason": "complete"}}) + "\n\n"


@pytest.mark.asyncio
async def test_delegate_agents_threads_effort_into_the_child_request(tmp_path, monkeypatch):
    import src.agent_loop as al
    import src.ai_interaction as ai
    from src import tool_execution as te

    seen_calls = []

    async def _loop(endpoint_url, model, messages, **kwargs):
        seen_calls.append({"messages": messages, "gen_overrides": kwargs.get("gen_overrides")})
        text = messages[0]["content"]
        if "task low" in text:
            yield _harness_summary(["src/a.py"])
        elif "task high" in text:
            yield _harness_summary(["src/b.py"])
        else:
            yield _harness_summary(["src/c.py"])
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_agent_loop", _loop)
    monkeypatch.setattr(te, "get_active_workspace", lambda: str(tmp_path))
    monkeypatch.setattr(te, "get_active_workspace_roots", lambda: ())
    parent = type("P", (), {
        "endpoint_url": "http://127.0.0.1:8081/v1", "model": "qwen3.8-27b-q8-llamacpp",
        "headers": None, "name": "parent",
    })()
    sm = _SM()
    sm.sessions["parent"] = parent
    monkeypatch.setattr(ai, "get_session_manager", lambda: sm)

    tool = st.DelegateAgentsTool()
    result = await tool.execute(json.dumps({
        "tasks": [
            {"name": "L", "instruction": "task low", "effort": "low", "files": ["src/a.py"]},
            {"name": "H", "instruction": "task high", "effort": "high", "files": ["src/b.py"]},
            {"name": "D", "instruction": "task default", "files": ["src/c.py"]},
        ],
        "parallel": True,
    }), {"session_id": "parent", "owner": None, "progress_cb": None})

    by_name = {r["name"]: r for r in result["subagents"]}
    assert by_name["L"]["effort"] == "low"
    assert by_name["H"]["effort"] == "high"
    assert "effort" not in by_name["D"]

    calls_by_task = {}
    for call in seen_calls:
        content = call["messages"][0]["content"]
        for tag in ("task low", "task high", "task default"):
            if tag in content:
                calls_by_task[tag] = call
                break

    low_overrides = calls_by_task["task low"]["gen_overrides"]
    assert low_overrides is not None
    assert low_overrides["think"] is False
    assert low_overrides["reasoning_effort"] == "low"
    assert "Be brief" in calls_by_task["task low"]["messages"][0]["content"]

    high_overrides = calls_by_task["task high"]["gen_overrides"]
    assert high_overrides is not None
    assert high_overrides["think"] is True
    assert high_overrides["reasoning_effort"] == "high"
    assert high_overrides["reasoning_budget"] > effort_profile.DEFAULT_REASONING_BUDGET
    assert "Think carefully" in calls_by_task["task high"]["messages"][0]["content"]

    # Default path unchanged byte-for-byte: no `effort` -> gen_overrides is
    # exactly what the ctx provided (None, since the test passed none).
    assert calls_by_task["task default"]["gen_overrides"] is None
    assert "Be brief" not in calls_by_task["task default"]["messages"][0]["content"]
    assert "Think carefully" not in calls_by_task["task default"]["messages"][0]["content"]


@pytest.mark.asyncio
async def test_default_path_preserves_caller_gen_overrides_when_effort_omitted(tmp_path, monkeypatch):
    """A task with no `effort` must not perturb a gen_overrides the coordinator
    already set (e.g. from ctx["gen_overrides"], a saved per-model pin)."""
    import src.agent_loop as al
    import src.ai_interaction as ai
    from src import tool_execution as te

    seen = {}

    async def _loop(endpoint_url, model, messages, **kwargs):
        seen["gen_overrides"] = kwargs.get("gen_overrides")
        yield _harness_summary(["src/a.py"])
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_agent_loop", _loop)
    monkeypatch.setattr(te, "get_active_workspace", lambda: str(tmp_path))
    monkeypatch.setattr(te, "get_active_workspace_roots", lambda: ())
    parent = type("P", (), {
        "endpoint_url": "http://127.0.0.1:8081/v1", "model": "m", "headers": None, "name": "parent",
    })()
    sm = _SM()
    sm.sessions["parent"] = parent
    monkeypatch.setattr(ai, "get_session_manager", lambda: sm)

    tool = st.DelegateAgentsTool()
    pinned = {"top_p": 0.7}
    await tool.execute(json.dumps({"tasks": [{"name": "D", "instruction": "task default"}]}),
                        {"session_id": "parent", "owner": None, "progress_cb": None,
                         "gen_overrides": pinned})
    assert seen["gen_overrides"] == {"top_p": 0.7}


# ── 5. llm_core: an explicit reasoning_budget override reaches the wire ──

def test_llm_core_reasoning_budget_override_reaches_self_hosted_payload(monkeypatch):
    import src.llm_core as llm_core

    class _FakeResp:
        status_code = 200

        async def aiter_lines(self):
            yield "data: " + json.dumps({"choices": [{"delta": {"content": "ok"}}]})
            yield "data: [DONE]"

        async def aread(self):
            return b""

    class _FakeStreamCtx:
        async def __aenter__(self):
            return _FakeResp()

        async def __aexit__(self, *a):
            return False

    class _FakeClient:
        def __init__(self):
            self.payload = {}

        def stream(self, method, url, **kw):
            self.payload = kw.get("json") or {}
            return _FakeStreamCtx()

    client = _FakeClient()
    monkeypatch.setattr(llm_core, "_get_http_client", lambda: client)
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda u: False)
    monkeypatch.setattr(llm_core, "note_model_activity", lambda *a, **k: None)
    monkeypatch.setattr(llm_core, "_clear_host_dead", lambda *a, **k: None)

    async def run(gen_overrides=None):
        return [c async for c in llm_core.stream_llm(
            "http://127.0.0.1:8081/v1", "qwen3.8-27b-q8-llamacpp",
            [{"role": "user", "content": "hi"}],
            gen_overrides=gen_overrides,
        )]

    # "high": explicit think + a bigger-than-default budget.
    asyncio.run(run(gen_overrides={"think": True, "reasoning_budget": 8192}))
    assert client.payload["chat_template_kwargs"] == {"enable_thinking": True}
    assert client.payload["reasoning_budget"] == 8192

    # "low": thinking off entirely -> no reasoning_budget field at all,
    # regardless of what value was passed alongside it.
    asyncio.run(run(gen_overrides={"think": False, "reasoning_budget": 0}))
    assert client.payload["chat_template_kwargs"] == {"enable_thinking": False}
    assert "reasoning_budget" not in client.payload
