"""Assistant block content must remain blocks beside native tool calls."""
import copy

import pytest

from src import llm_core


def _history(content, *, thinking=None):
    call = {
        "id": "call1", "type": "function",
        "function": {"name": "read_file", "arguments": '{"path":"a.txt"}'},
        "extra_content": {"opaque_provider": {"token": "unchanged"}},
    }
    if thinking:
        call["extra_content"]["anthropic"] = {"thinking": thinking}
    return [
        {"role": "assistant", "content": content, "tool_calls": [call]},
        {"role": "tool", "tool_call_id": "call1", "content": "done"},
    ]


def _payload(history, *, tools=None):
    projected = llm_core._sanitize_llm_messages(history, provider="anthropic")
    return llm_core._build_anthropic_payload("claude", projected, 0.0, 100, tools=tools)


@pytest.mark.parametrize("with_cache", [False, True])
def test_text_blocks_with_answered_tool_call_remain_flat_and_keep_identity(with_cache):
    blocks = [
        {"type": "text", "text": "Inspecting file", "citations": []},
        {"type": "text", "text": "Next step"},
    ]
    history = _history(blocks)
    original = copy.deepcopy(history)
    tools = [{"type": "function", "function": {
        "name": "read_file", "parameters": {"type": "object", "properties": {}},
    }}] if with_cache else None
    payload = _payload(history, tools=tools)
    content = payload["messages"][0]["content"]
    assert content[:2] == blocks
    assert all(isinstance(block["text"], str) for block in content if block["type"] == "text")
    assert content[2]["type"] == "tool_use"
    assert content[2]["id"] == "call1"
    assert content[2]["input"] == {"path": "a.txt"}
    assert payload["messages"][1]["content"][0]["tool_use_id"] == "call1"
    assert history == original
    assert _payload(history, tools=tools) == payload


@pytest.mark.parametrize("content, expected", [
    ("Inspecting file", [{"type": "text", "text": "Inspecting file"}]),
    (None, []), ("", []), ([], []),
])
def test_legacy_prose_and_empty_content_keep_tool_call(content, expected):
    payload = _payload(_history(content))
    assert payload["messages"][0]["content"][:-1] == expected
    assert payload["messages"][0]["content"][-1]["id"] == "call1"


def test_signed_thinking_from_tool_metadata_precedes_text_blocks_unchanged():
    thinking = [
        {"type": "thinking", "thinking": "plan", "signature": "SIG=="},
        {"type": "redacted_thinking", "data": "opaque=="},
    ]
    blocks = [{"type": "text", "text": "Inspecting file"}]
    history = _history(blocks, thinking=thinking)
    original = copy.deepcopy(history)
    content = _payload(history)["messages"][0]["content"]
    assert content[:-1] == thinking + blocks
    assert content[-1]["id"] == "call1"
    assert history == original


def test_native_thinking_blocks_keep_signature_and_order():
    blocks = [
        {"type": "thinking", "thinking": "plan", "signature": "SIG=="},
        {"type": "redacted_thinking", "data": "opaque=="},
        {"type": "text", "text": "Inspecting file"},
    ]
    history = _history(blocks)
    original = copy.deepcopy(history)
    assert _payload(history)["messages"][0]["content"][:-1] == blocks
    assert history == original


def test_other_blocks_follow_existing_converter_passthrough_contract():
    # This only checks projection parity, not provider support for this block.
    blocks = [{"type": "unknown_fixture", "opaque": {"value": "unchanged"}}]
    history = _history(blocks)
    original = copy.deepcopy(history)
    assert _payload(history)["messages"][0]["content"][:-1] == llm_core._convert_openai_content_to_anthropic(blocks)
    assert history == original
