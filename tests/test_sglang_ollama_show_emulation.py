"""An SGLang server answers Ollama's /api/show with a fixed ["completion"] list.

Read as Ollama's own answer, that list made a vision-capable checkpoint served by
SGLang look text-only, so tool-result images were never shown to it.
"""
from src import chat_helpers as ch
from src import llm_core


class _Resp:
    def __init__(self, data, status=200):
        self._data = data
        self.status_code = status

    def json(self):
        return self._data


def _fake_post(data):
    return lambda *a, **k: _Resp(data)


SGLANG_SHOW = {"modelfile": "FROM m", "capabilities": ["completion"],
               "details": {"format": "sglang", "family": "m"}}
OLLAMA_SHOW = {"capabilities": ["completion", "vision", "tools"], "details": {"format": "gguf", "family": "qwen3"}}


def test_sglang_emulated_show_is_not_an_answer(monkeypatch):
    llm_core._ollama_caps_cache.clear()
    monkeypatch.setattr(llm_core.httpx, "post", _fake_post(SGLANG_SHOW))
    assert llm_core._ollama_model_caps("http://192.168.0.9:8888/v1", "m") is None


def test_real_ollama_show_still_counts(monkeypatch):
    llm_core._ollama_caps_cache.clear()
    monkeypatch.setattr(llm_core.httpx, "post", _fake_post(OLLAMA_SHOW))
    assert llm_core._ollama_model_caps("http://127.0.0.1:11434/v1", "q") == frozenset({"completion", "vision", "tools"})


def test_sglang_vision_falls_through_to_the_image_probe(monkeypatch):
    llm_core._ollama_caps_cache.clear()
    monkeypatch.setattr(llm_core.httpx, "post", _fake_post(SGLANG_SHOW))
    monkeypatch.setattr(ch, "lmstudio_supports_vision", lambda u, m: None)
    monkeypatch.setattr(ch, "llamacpp_supports_vision", lambda u: None)
    monkeypatch.setattr(ch, "is_known_llamacpp", lambda u: False)
    monkeypatch.setattr(ch, "openai_compat_supports_vision", lambda u, m: True)
    assert ch.ollama_supports_vision("http://192.168.0.9:8888/v1", "m") is None
    assert ch.model_supports_vision("deepseek-v4.1-flash", "http://192.168.0.9:8888/v1") is True


def test_sglang_text_only_model_stays_text_only(monkeypatch):
    llm_core._ollama_caps_cache.clear()
    monkeypatch.setattr(llm_core.httpx, "post", _fake_post(SGLANG_SHOW))
    monkeypatch.setattr(ch, "lmstudio_supports_vision", lambda u, m: None)
    monkeypatch.setattr(ch, "llamacpp_supports_vision", lambda u: None)
    monkeypatch.setattr(ch, "is_known_llamacpp", lambda u: False)
    monkeypatch.setattr(ch, "openai_compat_supports_vision", lambda u, m: False)
    assert ch.model_supports_vision("some-text-model", "http://192.168.0.9:8888/v1") is False
