"""An interrupted model response continues the existing task, with its tools."""
import copy
import json

import pytest

import src.agent_loop as al
from tests.test_agent_harness_loop import _collect, _events, _patch_common


@pytest.mark.parametrize("error_class,status", [
    ("transport.incomplete_stream", 502),
    ("transport.connection_reset", 502),
    ("timeout.deadline_exceeded", 504),
    ("timeout.inactivity", 504),
])
@pytest.mark.parametrize("cuts", [1, 4])
def test_partial_transport_failure_continues_without_repeating_prefix(
    monkeypatch, tmp_path, error_class, status, cuts,
):
    _patch_common(monkeypatch)
    requests = []

    async def stream(candidates, messages, **kwargs):
        requests.append(copy.deepcopy(messages))
        if len(requests) <= cuts:
            text = "La primera parte de la respuesta." if len(requests) == 1 else f"Fragmento adicional {len(requests)}."
            yield 'data: ' + json.dumps({"delta": text}) + '\n\n'
            yield 'event: error\ndata: ' + json.dumps({"error": "connection ended",
                "status": status, "error_class": error_class, "partial": True}) + '\n\n'
        else:
            yield 'data: ' + json.dumps({"delta": "La segunda parte de la respuesta."}) + '\n\n'
            yield 'data: {"type":"finish","finish_reason":"stop"}\n\n'
            yield 'data: [DONE]\n\n'

    monkeypatch.setattr(al, "stream_llm_with_fallback", stream)
    events = _events(_collect(al.stream_agent_loop(
        "https://model.example.test/v1", "test-model",
        [{"role": "user", "content": "Explica las dos partes de una respuesta."}],
        workspace=str(tmp_path), max_rounds=8, relevant_tools={"read_file"},
        harness_options={"no_memory": True, "no_skills": True, "repo_map": False, "review_model": "off"},
    )))
    assert len(requests) == cuts + 1
    assert any("La primera parte" in str(m.get("content")) for m in requests[1])
    assert any(e.get("reason") == "stream_interrupted" for e in events)
    assert not any(e.get("type") == "agent_terminal" for e in events)
    visible = "".join(e.get("delta", "") for e in events if not e.get("thinking"))
    assert visible.count("La primera parte") == 1
    assert visible.count("La segunda parte") == 1
    assert len([e for e in events if e.get("reason") == "stream_interrupted"]) == cuts


def test_tool_results_survive_interruption_and_are_not_reexecuted(monkeypatch, tmp_path):
    _patch_common(monkeypatch)
    requests, effects = [], []

    async def execute(block, *args, **kwargs):
        effects.append(block.tool_type)
        return block.tool_type, {"output": "saved-object-id=synthetic-436", "exit_code": 0}

    async def stream(candidates, messages, **kwargs):
        requests.append(copy.deepcopy(messages))
        if len(requests) == 1:
            yield 'data: ' + json.dumps({"type": "tool_calls", "calls": [{
                "id": "call436", "name": "read_file", "arguments": {"path": "synthetic.txt"},
            }]}) + '\n\n'
            yield 'data: {"type":"finish","finish_reason":"tool_calls"}\n\n'
        elif len(requests) == 2:
            yield 'data: {"delta":"El resultado ya obtenido permite explicar "}\n\n'
            yield 'event: error\ndata: {"error":"reset","status":502,"error_class":"transport.connection_reset","partial":true}\n\n'
        else:
            yield 'data: {"delta":"la estructura completa del documento."}\n\n'
            yield 'data: {"type":"finish","finish_reason":"stop"}\n\n'
        yield 'data: [DONE]\n\n'

    monkeypatch.setattr(al, "execute_tool_block", execute)
    monkeypatch.setattr(al, "stream_llm_with_fallback", stream)
    events = _events(_collect(al.stream_agent_loop(
        "https://model.example.test/v1", "test-model",
        [{"role": "user", "content": "Lee synthetic.txt y explica su estructura."}],
        workspace=str(tmp_path), max_rounds=5, relevant_tools={"read_file"},
        harness_options={"no_memory": True, "no_skills": True, "repo_map": False, "review_model": "off"},
    )))
    assert len(requests) == 3
    assert effects == ["read_file"]
    assert "saved-object-id=synthetic-436" in str(requests[2])
    assert not any(e.get("type") == "agent_terminal" for e in events)


@pytest.mark.parametrize("error", [
    {"status": 401, "error_class": "permission.denied", "partial": True},
    {"status": 502, "error_class": "unknown.unmapped_exception", "partial": True},
    {"status": 504, "error_class": "timeout.deadline_exceeded", "partial": False},
])
def test_definite_failure_is_not_reclassified_as_a_partial_connection(monkeypatch, tmp_path, error):
    _patch_common(monkeypatch)
    requests = []

    async def stream(*args, **kwargs):
        requests.append(True)
        yield 'data: {"delta":"Parte inicial"}\n\n'
        yield 'event: error\ndata: ' + json.dumps({"error": "definite failure", **error}) + '\n\n'

    monkeypatch.setattr(al, "stream_llm_with_fallback", stream)
    events = _events(_collect(al.stream_agent_loop(
        "https://model.example.test/v1", "test-model",
        [{"role": "user", "content": "Explica una respuesta."}],
        workspace=str(tmp_path), max_rounds=5, relevant_tools={"read_file"},
        harness_options={"no_memory": True, "no_skills": True, "repo_map": False, "review_model": "off"},
    )))
    assert len(requests) == 1
    assert any(e.get("type") == "agent_terminal" for e in events)


def test_stop_during_continuation_prevents_another_model_request(monkeypatch, tmp_path):
    _patch_common(monkeypatch)
    requests, stopped, events = [], [False], []

    async def stream(*args, **kwargs):
        requests.append(True)
        yield 'data: {"delta":"Parte inicial"}\n\n'
        yield 'event: error\ndata: {"error":"reset","status":502,"error_class":"transport.connection_reset","partial":true}\n\n'

    monkeypatch.setattr(al, "stream_llm_with_fallback", stream)

    async def consume():
        async for chunk in al.stream_agent_loop(
            "https://model.example.test/v1", "test-model",
            [{"role": "user", "content": "Explica una respuesta."}],
            workspace=str(tmp_path), max_rounds=5, relevant_tools={"read_file"},
            pending_cancel=lambda: "task_cancelled" if stopped[0] else None,
            harness_options={"no_memory": True, "no_skills": True, "repo_map": False, "review_model": "off"},
        ):
            parsed = _events([chunk])
            events.extend(parsed)
            if any(e.get("reason") == "stream_interrupted" for e in parsed):
                stopped[0] = True

    import asyncio
    asyncio.run(consume())
    assert stopped[0]
    assert len(requests) == 1
    assert any(e.get("type") == "cancelled" for e in events)
