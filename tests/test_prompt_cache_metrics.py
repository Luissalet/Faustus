"""The turn's metrics carry prompt-cache reuse across its rounds: tokens the
server processed, tokens it reused, and rounds whose cache stopped short of
the previous request (an earlier message was rewritten)."""
import json

import src.agent_loop as al
from tests.test_agent_harness_loop import _collect, _events


def test_metrics_sum_cache_use_and_count_lost_rounds(tmp_path, monkeypatch):
    monkeypatch.setattr(al, "get_setting", lambda k, d=None: d, raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)
    monkeypatch.setattr(al, "blocked_tools_for_owner", lambda owner: set(), raising=False)

    async def _fake_exec(block, *a, **k):
        return (block.tool_type, {"output": "x = 1", "exit_code": 0})
    monkeypatch.setattr(al, "execute_tool_block", _fake_exec, raising=False)
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    # (prompt_n, cache_n) per round: round 2 reuses all of round 1's prompt,
    # round 3 reuses less than round 2's -> one lost round.
    timings = [(1000, 0), (200, 1000), (900, 500)]
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
        p, c = timings[min(n, len(timings) - 1)]
        usage = {"type": "usage", "data": {
            "input_tokens": p + c, "output_tokens": 5,
            "engine_timings": {"prompt_n": p, "cache_n": c, "prompt_ms": 10.0,
                               "predicted_n": 5, "predicted_ms": 5.0, "load_ms": None,
                               "source": "llamacpp"}}}
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
    assert metrics[-1]["prompt_cache"] == {"rounds": 3, "processed": 2100, "cached": 1500, "lost_rounds": 1}
