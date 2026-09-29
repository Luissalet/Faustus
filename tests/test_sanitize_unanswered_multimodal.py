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
