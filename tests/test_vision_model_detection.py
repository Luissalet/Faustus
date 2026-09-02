"""Tests for is_vision_model (issue #124).

Local vision models served through Ollama/llama.cpp show up under many
names. If one isn't recognized as vision-capable, the image attachment is
stripped from the request before it reaches the model, so it silently never
sees the picture.
"""
from src.chat_helpers import is_vision_model


def test_recognizes_local_and_hosted_vision_models():
    for name in [
        # the ones #124 missed
        "moondream", "moondream:latest",
        "llama3.2-vision:11b", "granite3.2-vision",
        "qwen2.5-vl:7b", "qwen2.5vl", "internvl2.5", "cogvlm",
        # already worked, keep them working
        "llava", "llava:7b", "bakllava", "minicpm-v",
        "gpt-4o", "claude-sonnet-4", "gemini-2.0-flash", "pixtral-12b",
    ]:
        assert is_vision_model(name), f"{name!r} should be detected as vision-capable"


def test_text_only_models_not_flagged():
    for name in ["qwen2.5:3b", "mistral", "llama3.1:8b", "deepseek-r1", "phi3", "vicuna", ""]:
        assert not is_vision_model(name), f"{name!r} should not be flagged as vision"


def test_none_is_safe():
    assert is_vision_model(None) is False


def test_recognizes_multimodal_families_without_vision_in_name():
    # issue #1274: these are vision-capable but their names don't contain
    # "vision"/"vl", so they were dropped and the model never saw the image.
    for name in [
        "gemma3:4b", "gemma3", "gemma-3-27b-it",
        "llama4:scout", "llama4", "llama-4-maverick",
        "mistral-small3.1", "mistral-small-3.2",
        "phi-4-multimodal", "phi4-multimodal",
    ]:
        assert is_vision_model(name), f"{name!r} should be detected as vision-capable"


def test_new_keywords_do_not_overmatch_text_models():
    # The added families must not flag their text-only siblings.
    for name in ["gemma2:9b", "gemma:7b", "llama3.3", "mistral-small", "phi-3-mini"]:
        assert not is_vision_model(name), f"{name!r} should not be flagged as vision"


# ── Ollama /api/show capabilities (FAUSTUS) ───────────────────────────────
#
# qwen3.5:9b reports capabilities ["completion", "vision", "tools",
# "thinking"] from Ollama /api/show, but its name says nothing about vision,
# so the heuristic alone returned False and every screenshot was silently
# swapped for a caption. The server's own answer wins when it is available;
# the heuristic stays as the fallback for anything else.

import pytest

from src import chat_helpers
from src.chat_helpers import model_supports_vision


@pytest.fixture
def _no_lmstudio(monkeypatch):
    monkeypatch.setattr(chat_helpers, "lmstudio_supports_vision", lambda url, model: None)


def _caps(monkeypatch, caps):
    calls = []

    def _fake(url, model):
        calls.append((url, model))
        return caps

    import src.llm_core as llm_core
    monkeypatch.setattr(llm_core, "_ollama_model_caps", _fake)
    return calls


def test_qwen35_name_heuristic_safety_net():
    for name in ["qwen3.5:9b", "qwen3.5:27b", "qwen3.8:4b"]:
        assert is_vision_model(name), name


def test_ollama_reported_vision_wins_over_name(monkeypatch, _no_lmstudio):
    calls = _caps(monkeypatch, frozenset({"completion", "vision", "tools"}))
    assert model_supports_vision("nameless-model:9b", "http://127.0.0.1:11434/v1") is True
    assert calls == [("http://127.0.0.1:11434/v1", "nameless-model:9b")]


def test_ollama_reported_text_only_wins_over_name(monkeypatch, _no_lmstudio):
    _caps(monkeypatch, frozenset({"completion", "tools"}))
    assert model_supports_vision("llava:7b", "http://localhost:11434/v1") is False


def test_ollama_native_api_url_is_probed_too(monkeypatch, _no_lmstudio):
    calls = _caps(monkeypatch, frozenset({"completion", "vision"}))
    assert model_supports_vision("plain:1b", "http://localhost:11434/api/chat") is True
    assert calls


def test_ollama_unknown_caps_fall_back_to_heuristic(monkeypatch, _no_lmstudio):
    _caps(monkeypatch, None)  # server down / model missing
    assert model_supports_vision("llava:7b", "http://127.0.0.1:11434/v1") is True
    assert model_supports_vision("qwen2.5:3b", "http://127.0.0.1:11434/v1") is False


def test_ollama_empty_caps_fall_back_to_heuristic(monkeypatch, _no_lmstudio):
    # An old Ollama that does not report `capabilities` at all.
    _caps(monkeypatch, frozenset())
    assert model_supports_vision("llava:7b", "http://127.0.0.1:11434/v1") is True


def test_ollama_probe_failure_is_swallowed(monkeypatch, _no_lmstudio):
    import src.llm_core as llm_core

    def _boom(url, model):
        raise RuntimeError("network down")

    monkeypatch.setattr(llm_core, "_ollama_model_caps", _boom)
    assert model_supports_vision("llava:7b", "http://127.0.0.1:11434/v1") is True
    assert model_supports_vision("qwen2.5:3b", "http://127.0.0.1:11434/v1") is False


def test_remote_endpoints_are_not_probed(monkeypatch, _no_lmstudio):
    calls = _caps(monkeypatch, frozenset({"completion", "vision"}))
    assert model_supports_vision("gpt-4.1", "https://api.openai.com/v1") is True
    assert model_supports_vision("mystery", "https://api.openai.com/v1") is False
    assert calls == []


def test_no_endpoint_uses_heuristic_only(monkeypatch):
    calls = _caps(monkeypatch, frozenset({"completion", "vision"}))
    assert model_supports_vision("mystery", "") is False
    assert calls == []
