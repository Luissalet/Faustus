"""Regression coverage for the plain-chat local-temperature-floor defect.

`chat_mode == "chat"` (plain conversation, no tools) calls
`stream_llm_with_fallback` directly with `temperature=ctx.preset.temperature`,
bypassing `_stream_agent_loop_body` entirely — so it never went through
`local_temperature_floor`. `ctx.preset.temperature` is
`LLMConfig.DEFAULT_TEMPERATURE` (1.0) whenever nothing explicit set it
(`src/chat_handler.py::validate_and_extract_preset`), and because that value
already lands in the payload as an explicit key, `_apply_local_generation_
stability`'s `setdefault` in llm_core never fires. Verified live against a
self-hosted llama-server: chat-mode turns ran at temperature=1.0 with
`local_temperature_default=0.6` configured.

The rule: a local endpoint gets `local_temperature_default` unless something
represents a REAL choice — a per-turn `/temp` override (gen_overrides) or a
preset that authored its own "temperature" key. A bare DEFAULT_TEMPERATURE
fallback is not a choice and must not shadow the floor.
"""
from types import SimpleNamespace

import pytest

from src.chat_handler import ChatHandler
import routes.chat_helpers as chat_helpers
import src.llm_core as llm_core
from tests.test_foreground_model_routing import _RouteRequest, _chat_stream_endpoint


# ---------------------------------------------------------------------------
# src/llm_core.local_temperature_floor — the shared decision function
# ---------------------------------------------------------------------------

def test_floor_applies_for_local_endpoint_with_nothing_explicit(monkeypatch):
    monkeypatch.setattr(
        "src.settings.get_setting",
        lambda key, default=None: 0.6 if key == "local_temperature_default" else default,
    )
    assert llm_core.local_temperature_floor("http://127.0.0.1:8081/v1", False) == 0.6


def test_floor_is_none_when_temperature_is_explicit(monkeypatch):
    monkeypatch.setattr(
        "src.settings.get_setting",
        lambda key, default=None: 0.6 if key == "local_temperature_default" else default,
    )
    assert llm_core.local_temperature_floor("http://127.0.0.1:8081/v1", True) is None


def test_floor_is_none_for_a_remote_endpoint(monkeypatch):
    monkeypatch.setattr(
        "src.settings.get_setting",
        lambda key, default=None: 0.6 if key == "local_temperature_default" else default,
    )
    assert llm_core.local_temperature_floor("https://api.openai.com/v1", False) is None


def test_floor_is_none_when_setting_is_zero_or_empty(monkeypatch):
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: default)
    assert llm_core.local_temperature_floor("http://127.0.0.1:8081/v1", False) is None


# ---------------------------------------------------------------------------
# Preset-explicit propagation (validate_and_extract_preset -> PresetInfo)
# ---------------------------------------------------------------------------

def test_preset_without_temperature_key_is_not_explicit():
    handler = ChatHandler.__new__(ChatHandler)
    handler.preset_manager = SimpleNamespace(presets={"p1": {"system_prompt": "hi"}})
    temperature, max_tokens, prompt, name, explicit = handler.validate_and_extract_preset("p1")
    assert temperature == 1.0  # DEFAULT_TEMPERATURE — a fallback, not a choice
    assert explicit is False


def test_preset_with_temperature_key_is_explicit():
    handler = ChatHandler.__new__(ChatHandler)
    handler.preset_manager = SimpleNamespace(presets={"p1": {"temperature": 0.9}})
    temperature, max_tokens, prompt, name, explicit = handler.validate_and_extract_preset("p1")
    assert temperature == 0.9
    assert explicit is True


def test_no_preset_id_is_not_explicit():
    handler = ChatHandler.__new__(ChatHandler)
    handler.preset_manager = SimpleNamespace(presets={})
    temperature, max_tokens, prompt, name, explicit = handler.validate_and_extract_preset(None)
    assert temperature == 1.0
    assert explicit is False


def test_extract_preset_threads_the_explicit_flag_through():
    handler = ChatHandler.__new__(ChatHandler)
    handler.preset_manager = SimpleNamespace(presets={"p1": {"temperature": 0.9}})
    info = chat_helpers.extract_preset(handler, "p1")
    assert info.temperature == 0.9
    assert info.temperature_explicit is True

    handler.preset_manager = SimpleNamespace(presets={})
    info2 = chat_helpers.extract_preset(handler, None)
    assert info2.temperature == 1.0
    assert info2.temperature_explicit is False


# ---------------------------------------------------------------------------
# Full route wiring: plain chat turn on a local endpoint
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_plain_local_chat_turn_gets_the_local_temperature_default(monkeypatch):
    """Whole-payload regression: nothing explicit set on a local endpoint ->
    stream_llm_with_fallback must be called with the configured
    local_temperature_default, not the preset's raw 1.0."""
    captured = {}
    endpoint = _chat_stream_endpoint(
        monkeypatch, "chat", captured,
        endpoint_url="http://127.0.0.1:8081/v1",
        capture_completion=False,
    )

    # The fixture's context.preset has no temperature_explicit attribute
    # (SimpleNamespace) -> getattr(..., False) treats it as a mere default,
    # exactly like validate_and_extract_preset's DEFAULT_TEMPERATURE case.
    import src.settings as settings
    monkeypatch.setattr(
        settings, "get_setting",
        lambda key, default=None: 0.6 if key == "local_temperature_default" else default,
    )

    captured_kwargs = {}
    orig_fake_chat_stream = None
    import routes.chat_routes as chat_routes

    async def fake_chat_stream(candidates, messages, **kwargs):
        captured_kwargs.update(kwargs)
        captured["chat"] = candidates
        import json
        yield f'data: {json.dumps({"delta": "done"})}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(chat_routes, "stream_llm_with_fallback", fake_chat_stream)

    response = await endpoint(_RouteRequest("chat"))
    async for _ in response.body_iterator:
        pass

    assert captured_kwargs.get("temperature") == 0.6


@pytest.mark.asyncio
async def test_plain_remote_chat_turn_is_unaffected(monkeypatch):
    captured = {}
    endpoint = _chat_stream_endpoint(
        monkeypatch, "chat", captured,
        endpoint_url="https://api.openai.com/v1",
    )

    import src.settings as settings
    monkeypatch.setattr(
        settings, "get_setting",
        lambda key, default=None: 0.6 if key == "local_temperature_default" else default,
    )

    captured_kwargs = {}
    import routes.chat_routes as chat_routes

    async def fake_chat_stream(candidates, messages, **kwargs):
        captured_kwargs.update(kwargs)
        import json
        yield f'data: {json.dumps({"delta": "done"})}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(chat_routes, "stream_llm_with_fallback", fake_chat_stream)

    response = await endpoint(_RouteRequest("chat"))
    async for _ in response.body_iterator:
        pass

    # Fixture's preset.temperature is 0.2 — a remote endpoint keeps it as-is.
    assert captured_kwargs.get("temperature") == 0.2
