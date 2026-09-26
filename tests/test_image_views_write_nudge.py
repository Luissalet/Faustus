"""After 2 x vision_write_every images looked at with nothing written, the
turn gets one harness note asking for the current best answer — sent after
the round's tool results, never between a call and its result."""
import json

import src.agent_loop as al
from tests.test_agent_harness_loop import _collect, _events

PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="


def _run(monkeypatch, tmp_path, writes_after=None):
    settings = {"vision_write_every": 1, "agent_tool_images": True, "tool_approval_mode": "auto"}
    monkeypatch.setattr(al, "get_setting", lambda k, d=None: settings.get(k, d), raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)
    monkeypatch.setattr(al, "blocked_tools_for_owner", lambda owner: set(), raising=False)

    async def _fake_exec(block, *a, **k):
        if block.tool_type == "write_file":
            return (block.tool_type, {"success": True, "path": "RESPUESTA.md"})
        return (block.tool_type, {"output": "a crop", "exit_code": 0,
                                  "images": [{"data": PNG, "mimeType": "image/png"}]})
    monkeypatch.setattr(al, "execute_tool_block", _fake_exec, raising=False)
    for i in range(4):
        (tmp_path / f"c{i}.png").write_bytes(b"x")
    calls = [[{"name": "read_file", "arguments": json.dumps({"path": f"c{i}.png"})}] for i in range(4)]
    if writes_after is not None:
        calls.insert(writes_after, [{"name": "write_file", "arguments": json.dumps({"path": "RESPUESTA.md", "content": "x"})}])
    script = calls + ["Hecho."]
    seen = []

    async def _fake_stream(_c, messages, **kwargs):
        seen.append([dict(m) for m in messages])
        item = script[min(len(seen) - 1, len(script) - 1)]
        if isinstance(item, str):
            yield f'data: {json.dumps({"delta": item})}\n\n'
            yield f'data: {json.dumps({"type": "finish", "finish_reason": "stop"})}\n\n'
        else:
            yield f'data: {json.dumps({"type": "tool_calls", "calls": item})}\n\n'
            yield f'data: {json.dumps({"type": "finish", "finish_reason": "tool_calls"})}\n\n'
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    _last_events[:] = _events(_collect(al.stream_agent_loop(
        "http://127.0.0.1:8081/v1", "qwen3-vl:8b",
        [{"role": "user", "content": "Mira los recortes y dime qué ves"}],
        max_rounds=8, relevant_tools={"read_file", "write_file"}, workspace=str(tmp_path),
    )))
    return seen


_last_events = []


def _notes(msgs):
    return [i for i, m in enumerate(msgs) if "images since you last wrote" in str(m.get("content"))]


def test_a_note_after_two_images_and_after_the_results(tmp_path, monkeypatch):
    seen = _run(monkeypatch, tmp_path)
    final = seen[-1]
    idx = _notes(final)
    assert len(idx) == 2, [str(m.get("content"))[:60] for m in final]
    for i in idx:
        prev = final[i - 1]
        assert prev.get("role") != "assistant" or not prev.get("tool_calls")

