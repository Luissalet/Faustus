"""Delegation keeps running until completion, an explicit bound, or Stop."""
import asyncio
import json

import pytest

from src.agent_tools import subagent_tools as st


def test_no_implicit_worker_deadline(monkeypatch):
    monkeypatch.setattr(st, "_setting", lambda k, d=None: d)
    parsed = st.parse_delegation_args(json.dumps({"tasks": ["finish the long job"]}))
    assert parsed["timeout_s"] == 0


def test_zero_and_positive_worker_deadlines_are_preserved(monkeypatch):
    monkeypatch.setattr(st, "_setting", lambda k, d=None: d)
    parsed = st.parse_delegation_args(json.dumps({
        "tasks": [{"instruction": "A", "timeout_s": 0},
                  {"instruction": "B", "timeout_s": 9000}], "timeout_s": 0,
    }))
    assert parsed["timeout_s"] == 0
    assert [task["timeout_s"] for task in parsed["tasks"]] == [0, 9000]


@pytest.mark.parametrize("width,count", [(0, 135), (130, 130), (12, 12)])
def test_width_is_requested_not_silently_capped(monkeypatch, width, count):
    monkeypatch.setattr(st, "_setting", lambda k, d=None: width if k == "agent_subagent_max_tasks" else d)
    parsed = st.parse_delegation_args(json.dumps({"tasks": [f"task {i}" for i in range(135)]}))
    assert len(parsed["tasks"]) == count
    assert parsed["dropped_tasks"] == 135 - count


@pytest.fixture
def worker(monkeypatch, tmp_path):
    import src.ai_interaction as ai
    import src.agent_loop as al
    from src import tool_execution as te
    from tests.test_subagents_v2 import _SM

    sm = _SM()
    sm.sessions["parent"] = type("Parent", (), {
        "endpoint_url": "http://192.168.0.236:8002/v1", "model": "configured-spark-model",
        "headers": None, "name": "parent",
    })()
    monkeypatch.setattr(ai, "get_session_manager", lambda: sm)
    monkeypatch.setattr(te, "get_active_workspace", lambda: str(tmp_path))
    monkeypatch.setattr(te, "get_active_workspace_roots", lambda: ())
    values = {"agent_subagent_tick_seconds": .01, "agent_subagent_stall_seconds": .02}
    monkeypatch.setattr(st, "_setting", lambda k, d=None: values.get(k, d))
    calls = []

    async def loop(endpoint_url, model, messages, **kwargs):
        calls.append((endpoint_url, model))
        await asyncio.sleep(.12)
        yield "data: " + json.dumps({"type": "harness_summary", "data": {
            "stop_reason": "complete", "mutations": [],
        }}) + "\n\n"
        yield "data: " + json.dumps({"choices": [{"delta": {"content": "verified result"}}]}) + "\n\n"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_agent_loop", loop)
    return values, calls


@pytest.mark.asyncio
async def test_silent_worker_completes_on_inherited_spark_route(worker):
    _, calls = worker
    result = await st.DelegateAgentsTool().execute(json.dumps({
        "tasks": ["inspect the result"], "timeout_s": 0,
    }), {"session_id": "parent"})
    assert result["subagents"][0]["stop_reason"] == "complete"
    assert calls == [("http://192.168.0.236:8002/v1", "configured-spark-model")]


@pytest.mark.asyncio
async def test_explicit_short_deadline_stops_worker(worker):
    result = await st.DelegateAgentsTool().execute(json.dumps({
        "tasks": ["inspect the result"], "timeout_s": .03,
    }), {"session_id": "parent"})
    assert result["subagents"][0]["stop_reason"] == "timeout"


@pytest.mark.asyncio
async def test_requested_supervisor_still_stops_stalled_worker(worker):
    values, _ = worker
    values["agent_subagent_supervisor"] = True
    result = await st.DelegateAgentsTool().execute(json.dumps({"tasks": ["inspect the result"]}),
                                                 {"session_id": "parent"})
    assert result["subagents"][0]["stop_reason"] == "stalled"


@pytest.mark.asyncio
async def test_task_zero_overrides_parent_deadline(worker):
    result = await st.DelegateAgentsTool().execute(json.dumps({
        "tasks": [{"instruction": "inspect the result", "timeout_s": 0}], "timeout_s": .03,
    }), {"session_id": "parent"})
    assert result["subagents"][0]["stop_reason"] == "complete"


@pytest.mark.asyncio
async def test_worker_report_uses_canonical_response_replacement(worker, monkeypatch):
    import src.agent_loop as al

    async def corrected(endpoint_url, model, messages, **kwargs):
        for event in ({"delta": "The value is wrong."},
                      {"type": "response_replace", "text": ""},
                      {"delta": "The checked value is 42."},
                      {"type": "response_replace", "text": "Verified: 42."},
                      {"type": "harness_summary", "data": {"stop_reason": "complete", "mutations": []}}):
            yield 'data: ' + json.dumps(event) + '\n\n'
        yield 'data: [DONE]\n\n'

    monkeypatch.setattr(al, 'stream_agent_loop', corrected)
    result = await st.DelegateAgentsTool().execute(json.dumps({"tasks": ["Read and report the value"]}),
                                                 {"session_id": "parent"})
    assert result['subagents'][0]['final_text'] == 'Verified: 42.'
    assert 'wrong' not in result['output']


@pytest.mark.asyncio
async def test_worker_report_does_not_keep_guessed_pre_tool_result(worker, monkeypatch):
    import src.agent_loop as al

    async def corrected(endpoint_url, model, messages, **kwargs):
        for event in ({"delta": "Guessed value: wrong."},
                      {"type": "tool_output", "tool": "read_file", "result_status": "success", "output": "42"},
                      {"delta": "Verified value: 42."},
                      {"type": "harness_summary", "data": {"stop_reason": "complete", "mutations": []}}):
            yield 'data: ' + json.dumps(event) + '\n\n'
        yield 'data: [DONE]\n\n'

    monkeypatch.setattr(al, 'stream_agent_loop', corrected)
    result = await st.DelegateAgentsTool().execute(json.dumps({"tasks": ["Read and report the value"]}),
                                                 {"session_id": "parent"})
    assert result['subagents'][0]['final_text'] == 'Verified value: 42.'
    assert result['subagents'][0]['tool_calls'] == 1
