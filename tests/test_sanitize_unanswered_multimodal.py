"""Provider repair must preserve content and leave canonical history intact."""
from copy import deepcopy

import pytest

from src.llm_core import _sanitize_llm_messages


@pytest.mark.parametrize("provider", [None, "ollama", "anthropic"])
@pytest.mark.parametrize("content", [
    [{"type": "text", "text": "Partial analysis"}],
    [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}],
])
def test_unanswered_call_preserves_assistant_blocks_and_source(provider, content):
    history = [{"role": "assistant", "content": content, "tool_calls": [
        {"id": "pending", "type": "function", "function": {
            "name": "read_file", "arguments": '{"path":"example.txt"}'}}
    ]}, {"role": "user", "content": "Continue"}]
    original = deepcopy(history)
    projected = _sanitize_llm_messages(history, provider=provider)
    assert projected == [
        {"role": "assistant", "content": content},
        {"role": "user", "content": "Continue"},
    ]
    assert history == original
    assert _sanitize_llm_messages(projected, provider=provider) == projected


@pytest.mark.parametrize("content", [None, "", "  ", []])
def test_unanswered_empty_assistant_is_still_omitted(content):
    history = [{"role": "assistant", "content": content, "tool_calls": [
        {"id": "pending", "type": "function", "function": {
            "name": "read_file", "arguments": "{}"}}
    ]}]
    original = deepcopy(history)
    assert _sanitize_llm_messages(history) == []
    assert history == original


@pytest.mark.parametrize("provider", [None, "ollama", "anthropic"])
@pytest.mark.parametrize("pending", [False, True])
@pytest.mark.parametrize("marker", ["Reference context received.", "<<faustus_ctx_ack>>"])
def test_boundary_text_with_image_remains_multimodal_and_idempotent(provider, pending, marker):
    content = [
        {"type": "text", "text": marker},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]
    history = [{"role": "assistant", "content": content}]
    if pending:
        history[0]["tool_calls"] = [{"id": "pending", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]
    original = deepcopy(history)
    projected = _sanitize_llm_messages(history, provider=provider)
    assert projected == [{"role": "assistant", "content": content}]
    assert _sanitize_llm_messages(projected, provider=provider) == projected
    assert history == original


def test_legacy_boundary_pure_string_is_still_normalized():
    original = [{"role": "assistant", "content": "Reference context received."}]
    assert _sanitize_llm_messages(original) == [{"role": "assistant", "content": "<<faustus_ctx_ack>>"}]
    assert original[0]["content"] == "Reference context received."
