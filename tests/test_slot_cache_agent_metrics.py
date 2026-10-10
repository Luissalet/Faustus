"""An agent turn folds the pinned-document cache record of its rounds into
`metrics.prefix_cache` (src/slot_cache.py, #254): the first round saved the
slot, the later ones found it warm, the turn says `saved` once."""
import json

import src.agent_loop as al
from tests.test_agent_harness_loop import _collect, _events


def test_the_turn_metrics_carry_one_prefix_cache_record(tmp_path, monkeypatch):
    monkeypatch.setattr(al, "get_setting", lambda k, d=None: d, raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)
    monkeypatch.setattr(al, "blocked_tools_for_owner", lambda owner: set(), raising=False)

    async def _fake_exec(block, *a, **k):
        return (block.tool_type, {"output": "x = 1", "exit_code": 0})
    monkeypatch.setattr(al, "execute_tool_block", _fake_exec, raising=False)
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    records = [
        {"status": "saved", "slot": 1, "prefix_tokens": 1800, "cache_n": 0, "prompt_n": 1900},
        {"status": "warm", "slot": 1, "prefix_tokens": 1800, "cache_n": 1850, "prefill_saved_ms": 90.0},
        {"status": "warm", "slot": 1, "prefix_tokens": 1800, "cache_n": 1900, "prefill_saved_ms": 95.0},
    ]
    script = [
        [{"name": "read_file", "arguments": json.dumps({"path": "a.py"})}],
        [{"name": "read_file", "arguments": json.dumps({"path": "a.py", "offset": 1})}],
        "x vale 1.",
    ]
    calls = []

    async def _fake_stream(_c, messages, **kwargs):
        calls.append(1)
        n = len(calls) - 1
        item = script[min(n, len(script) - 1)]
        usage = {"type": "usage", "data": {
            "input_tokens": 1900, "output_tokens": 5, "prefix_cache": records[min(n, len(records) - 1)],
            "engine_timings": {"prompt_n": 50, "cache_n": 1850, "prompt_ms": 10.0, "predicted_n": 5,
                               "predicted_ms": 5.0, "load_ms": None, "source": "llamacpp"}}}
        if isinstance(item, str):
            yield f'data: {json.dumps({"delta": item})}\n\n'
            yield f"data: {json.dumps(usage)}\n\n"
            yield f'data: {json.dumps({"type": "finish", "finish_reason": "stop"})}\n\n'
        else:
            yield f'data: {json.dumps({"type": "tool_calls", "calls": item})}\n\n'
            yield f"data: {json.dumps(usage)}\n\n"
            yield f'data: {json.dumps({"type": "finish", "finish_reason": "tool_calls"})}\n\n'
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    events = _events(_collect(al.stream_agent_loop(
        "http://127.0.0.1:8081/v1", "qwen3-coder:30b",
        [{"role": "user", "content": "¿Cuánto vale x en a.py?"}],
        max_rounds=5, relevant_tools={"read_file"}, workspace=str(tmp_path),
    )))
    metrics = [e["data"] for e in events if e.get("type") == "metrics"]
    assert metrics, [e.get("type") for e in events]
    pc = metrics[-1]["prefix_cache"]
    assert pc["status"] == "saved" and pc["rounds"] == 3
    assert pc["prefill_saved_ms"] == 185.0


def test_a_turn_without_a_pinned_document_has_no_prefix_cache_key(tmp_path, monkeypatch):
    monkeypatch.setattr(al, "get_setting", lambda k, d=None: d, raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)
    monkeypatch.setattr(al, "blocked_tools_for_owner", lambda owner: set(), raising=False)

    async def _fake_exec(block, *a, **k):
        return (block.tool_type, {"output": "x = 1", "exit_code": 0})
    monkeypatch.setattr(al, "execute_tool_block", _fake_exec, raising=False)
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    calls = []

    async def _fake_stream(_c, messages, **kwargs):
        calls.append(1)
        usage = {"type": "usage", "data": {"input_tokens": 10, "output_tokens": 2}}
        if len(calls) == 1:
            yield f'data: {json.dumps({"type": "tool_calls", "calls": [{"name": "read_file", "arguments": json.dumps({"path": "a.py"})}]})}\n\n'
            yield f"data: {json.dumps(usage)}\n\n"
            yield f'data: {json.dumps({"type": "finish", "finish_reason": "tool_calls"})}\n\n'
        else:
            yield f'data: {json.dumps({"delta": "hola"})}\n\n'
            yield f"data: {json.dumps(usage)}\n\n"
            yield f'data: {json.dumps({"type": "finish", "finish_reason": "stop"})}\n\n'
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    events = _events(_collect(al.stream_agent_loop(
        "http://127.0.0.1:8081/v1", "qwen3-coder:30b", [{"role": "user", "content": "¿Cuánto vale x en a.py?"}],
        max_rounds=5, relevant_tools={"read_file"}, workspace=str(tmp_path),
    )))
    metrics = [e["data"] for e in events if e.get("type") == "metrics"]
    assert metrics and "prefix_cache" not in metrics[-1]
