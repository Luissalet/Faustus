import asyncio
import json

import src.agent_loop as al


def _collect(gen):
    async def _run():
        return [chunk async for chunk in gen]
    return asyncio.run(_run())


def _events(chunks):
    result = []
    for chunk in chunks:
        for line in chunk.splitlines():
            if not line.startswith("data: ") or line[6:] == "[DONE]":
                continue
            try:
                result.append(json.loads(line[6:]))
            except (TypeError, ValueError):
                pass
    return result


def test_stream_agent_loop_does_not_mutate_caller_relevant_tools(monkeypatch):
    """The live loop may expand its local offer but must keep caller scope reusable."""
    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)
    monkeypatch.setattr(al, "blocked_tools_for_owner", lambda owner: set(), raising=False)
    monkeypatch.setattr(al._chat_mode, "is_lean", lambda: False)

    async def _fake_stream(_candidates, messages, **kwargs):
        yield "data: " + json.dumps({"delta": "Listo."}, ensure_ascii=False) + "\n\n"
        yield "data: " + json.dumps({"type": "finish", "finish_reason": "stop"}) + "\n\n"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    caller_scope = {"read_file"}
    events = _events(_collect(al.stream_agent_loop(
        "http://127.0.0.1:11434/v1", "stub-qwen",
        [{"role": "user", "content": "Responde: listo."}],
        max_rounds=2, relevant_tools=caller_scope,
        session_id="relevant-tools-copy-test",
    )))

    assert any(event.get("type") == "round_info" for event in events), events
    assert any(event.get("type") == "metrics" for event in events), events
    assert caller_scope == {"read_file"}
