"""Provider repair must preserve content and leave canonical history intact.

An unanswered call used to be deleted from the prompt. The canonical projection
keeps it, with an explicit unknown result (src/history_projection.py); the
previous behaviour stays available as ``llm_projection_mode = legacy`` and is
pinned here too.
"""
from copy import deepcopy

import pytest

from src import history_projection as hp
from src import settings as _settings
from src.llm_core import _sanitize_llm_messages


@pytest.fixture
def projection_mode(monkeypatch):
    real = _settings.get_setting

    def _set(mode):
        monkeypatch.setattr(
            _settings, "get_setting",
            lambda key, default=None: mode if key == "llm_projection_mode" else real(key, default))
    return _set


def _call(call_id="pending", name="read_file", args='{"path":"example.txt"}'):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": args}}


@pytest.mark.parametrize("provider", [None, "ollama", "anthropic"])
@pytest.mark.parametrize("content", [
    [{"type": "text", "text": "Partial analysis"}],
    [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}],
])
def test_legacy_mode_drops_unanswered_call_keeps_assistant_blocks(projection_mode, provider, content):
    projection_mode("legacy")
    history = [{"role": "assistant", "content": content, "tool_calls": [_call()]},
               {"role": "user", "content": "Continue"}]
    original = deepcopy(history)
    projected = _sanitize_llm_messages(history, provider=provider)
    assert projected == [
        {"role": "assistant", "content": content},
        {"role": "user", "content": "Continue"},
    ]
    assert history == original
    assert _sanitize_llm_messages(projected, provider=provider) == projected


@pytest.mark.parametrize("provider", [None, "ollama", "anthropic"])
@pytest.mark.parametrize("content", [
    [{"type": "text", "text": "Partial analysis"}],
    [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}],
])
def test_unanswered_call_stays_with_blocks_and_an_unknown_result(provider, content):
    history = [{"role": "assistant", "content": content, "tool_calls": [_call()]},
               {"role": "user", "content": "Continue"}]
    original = deepcopy(history)
    projected = _sanitize_llm_messages(history, provider=provider)
    assert projected == [
        {"role": "assistant", "content": content, "tool_calls": [_call()]},
        {"role": "tool", "tool_call_id": "pending", "content": hp.UNKNOWN_RESULT_TEXT},
        {"role": "user", "content": "Continue"},
    ]
    assert history == original
    assert _sanitize_llm_messages(projected, provider=provider) == projected


@pytest.mark.parametrize("content", [None, "", "  ", []])
def test_legacy_mode_unanswered_empty_assistant_is_omitted(projection_mode, content):
    projection_mode("legacy")
    history = [{"role": "assistant", "content": content, "tool_calls": [_call(args="{}")]}]
    original = deepcopy(history)
    assert _sanitize_llm_messages(history) == []
    assert history == original


@pytest.mark.parametrize("content", [None, "", "  ", []])
def test_unanswered_empty_assistant_call_is_not_deleted(content):
    history = [{"role": "assistant", "content": content, "tool_calls": [_call(args="{}")]}]
    original = deepcopy(history)
    projected = _sanitize_llm_messages(history)
    assert [m["role"] for m in projected] == ["assistant", "tool"]
    assert projected[0]["tool_calls"][0]["id"] == "pending"
    assert projected[1] == {"role": "tool", "tool_call_id": "pending", "content": hp.UNKNOWN_RESULT_TEXT}
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
        history[0]["tool_calls"] = [_call(args="{}")]
    original = deepcopy(history)
    projected = _sanitize_llm_messages(history, provider=provider)
    assert projected[0]["content"] == content
    assert (projected[0].get("tool_calls") is not None) is pending
    assert _sanitize_llm_messages(projected, provider=provider) == projected
    assert history == original


def test_legacy_boundary_pure_string_is_still_normalized():
    original = [{"role": "assistant", "content": "Reference context received."}]
    assert _sanitize_llm_messages(original) == [{"role": "assistant", "content": "<<faustus_ctx_ack>>"}]
    assert original[0]["content"] == "Reference context received."
