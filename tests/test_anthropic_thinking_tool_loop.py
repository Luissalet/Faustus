"""Claude thinks inside tool loops: signed thinking blocks ride on the first
tool call and go back before the tool_use on the next round."""
import json

from src import llm_core
from tests.test_llm_core_streaming_retries import _drive, _events, _Resp, _StreamCtx


def _line(obj):
    return "data: " + json.dumps(obj)


def test_stream_keeps_signed_thinking_on_the_first_tool_call(monkeypatch):
    lines = [
        _line({"type": "message_start", "message": {"model": "claude-sonnet-5",
               "usage": {"input_tokens": 10, "cache_read_input_tokens": 900, "cache_creation_input_tokens": 50}}}),
        _line({"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}}),
        _line({"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "Let me read "}}),
        _line({"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "the file."}}),
        _line({"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "SIG=="}}),
        _line({"type": "content_block_stop", "index": 0}),
        _line({"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "tu1", "name": "read_file"}}),
        _line({"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "{}"}}),
        _line({"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 20}}),
        _line({"type": "message_stop"}),
    ]
    _client, chunks = _drive(monkeypatch, [_StreamCtx(resp=_Resp(lines=lines))],
                             url="https://api.anthropic.com/v1/messages", model="claude-sonnet-5")
    events = _events(chunks)
    thinking = "".join(e["delta"] for e in events if e.get("thinking"))
    assert thinking == "Let me read the file."
    calls = next(e for e in events if e.get("type") == "tool_calls")["calls"]
    assert calls[0]["extra_content"] == {"anthropic": {"thinking": [
        {"type": "thinking", "thinking": "Let me read the file.", "signature": "SIG=="}]}}
    usage = next(e for e in events if e.get("type") == "usage")["data"]
    assert usage["cached_tokens"] == 900
    assert usage["prompt_cache"] == {"processed": 60, "cached": 900}


def _loop_messages(with_thinking=True):
    tc = {"id": "tu1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}
    if with_thinking:
        tc["extra_content"] = {"anthropic": {"thinking": [
            {"type": "thinking", "thinking": "plan", "signature": "SIG=="}]}}
    return [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "read it"},
        {"role": "assistant", "content": None, "tool_calls": [tc]},
        {"role": "tool", "tool_call_id": "tu1", "content": "body"},
    ]


def _tools():
    return [{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object", "properties": {}}}}]


def test_payload_replays_thinking_before_tool_use_and_allows_thinking():
    payload = llm_core._build_anthropic_payload("claude-sonnet-5", _loop_messages(), 0.2, 1000, tools=_tools())
    asst = [m for m in payload["messages"] if m["role"] == "assistant"][0]
    assert asst["content"][0] == {"type": "thinking", "thinking": "plan", "signature": "SIG=="}
    assert asst["content"][1]["type"] == "tool_use"
    assert llm_core._anthropic_tool_loop_allows_thinking(payload) is True


def test_loop_without_kept_thinking_stays_unthinking():
    payload = llm_core._build_anthropic_payload("claude-sonnet-5", _loop_messages(False), 0.2, 1000, tools=_tools())
    assert llm_core._anthropic_tool_loop_allows_thinking(payload) is False


def test_first_round_of_a_loop_may_think():
    payload = llm_core._build_anthropic_payload(
        "claude-sonnet-5", [{"role": "user", "content": "go"}], 0.2, 1000, tools=_tools())
    assert llm_core._anthropic_tool_loop_allows_thinking(payload) is True


def test_unsigned_thinking_is_never_replayed():
    assert llm_core._signed_anthropic_thinking({0: {"type": "thinking", "thinking": "x", "signature": ""}}) == []


def test_older_claude_with_tools_asks_for_interleaved_thinking(monkeypatch):
    from tests import test_llm_core_streaming_retries as h
    seen = {}

    class _Capture(h._SeqClient):
        def stream(self, method, url, **kwargs):
            seen["headers"] = kwargs.get("headers") or {}
            seen["json"] = kwargs.get("json") or {}
            return super().stream(method, url, **kwargs)

    lines = [_line({"type": "message_start", "message": {"model": "claude-sonnet-4-5", "usage": {"input_tokens": 3}}}),
             _line({"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}}),
             _line({"type": "message_stop"})]
    client = _Capture([_StreamCtx(resp=_Resp(lines=lines))])
    monkeypatch.setattr(llm_core, "_get_http_client", lambda: client)
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda u: False)
    monkeypatch.setattr(llm_core, "_clear_host_dead", lambda *a, **k: None)
    monkeypatch.setattr(llm_core, "note_model_activity", lambda *a, **k: None)
    monkeypatch.setattr(llm_core, "_mark_host_dead", lambda u: False)
    import asyncio

    async def run():
        return [c async for c in llm_core.stream_llm(
            "https://api.anthropic.com/v1/messages", "claude-sonnet-4-5", [{"role": "user", "content": "go"}],
            tools=_tools(), gen_overrides={"think": True, "reasoning_budget": 4096})]
    asyncio.run(run())
    assert seen["json"]["thinking"]["type"] == "enabled"
    assert "interleaved-thinking-2025-05-14" in seen["headers"].get("anthropic-beta", "")
