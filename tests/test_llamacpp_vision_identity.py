"""A llama-server is never taken for an Ollama, and a busy one keeps its answer.

Exam run 31 (26-09-2026): mid-round, the 27B's /props probe timed out, the
vision check fell through to Ollama's /api/show on the llama-server and then
to the family name ("qwen3.8" reads as multimodal). `count` sent the images
to the text-only 27B, and the call itself was rewritten to 8081/api/chat: 404.
"""
import httpx

import src.chat_helpers as ch


class _Resp:
    def __init__(self, data, ok=True):
        self._data, self.is_success = data, ok

    def json(self):
        return self._data


def _reset(monkeypatch):
    monkeypatch.setattr(ch, "_llamacpp_props_cache", {})
    monkeypatch.setattr(ch, "_llamacpp_identity", {})


def test_a_llamaserver_seen_once_is_never_an_ollama(monkeypatch):
    _reset(monkeypatch)
    monkeypatch.setattr(ch.httpx, "get", lambda url, timeout=0: _Resp(
        {"default_generation_settings": {}, "modalities": {"vision": False}}))
    url = "http://127.0.0.1:8081/v1"
    assert ch.llamacpp_supports_vision(url) is False
    assert ch.is_known_llamacpp(url)
    assert ch._is_local_ollama_url(url) is False
    assert ch._is_local_ollama_url("http://127.0.0.1:11434/v1") is True


def test_a_busy_server_keeps_its_last_answer(monkeypatch):
    _reset(monkeypatch)
    monkeypatch.setattr(ch.httpx, "get", lambda url, timeout=0: _Resp(
        {"default_generation_settings": {}, "modalities": {"vision": False}}))
    url = "http://127.0.0.1:8081/v1"
    assert ch.llamacpp_supports_vision(url) is False
    # the cache expires, and the server is now too busy to answer /props
    ch._llamacpp_props_cache[("127.0.0.1", 8081)] = (False, 0)

    def busy(url, timeout=0):
        raise httpx.ReadTimeout("busy")
    monkeypatch.setattr(ch.httpx, "get", busy)
    assert ch.llamacpp_supports_vision(url) is False
    assert ch.model_supports_vision("qwen3.8-27b-q8-llamacpp", url) is False


def test_a_managed_engine_is_known_without_the_network(monkeypatch):
    _reset(monkeypatch)
    import src.engine_swap as es
    monkeypatch.setattr(es, "engine_for_url", lambda url: {"id": "e1"} if ":8081" in url else None)

    def busy(url, timeout=0):
        raise httpx.ReadTimeout("busy")
    monkeypatch.setattr(ch.httpx, "get", busy)
    url = "http://127.0.0.1:8081/v1"
    assert ch.is_known_llamacpp(url)
    assert ch._is_local_ollama_url(url) is False
    assert ch.model_supports_vision("qwen3.8-27b-q8-llamacpp", url) is False


def test_the_vision_call_url_is_not_rewritten_for_a_llamaserver(monkeypatch):
    _reset(monkeypatch)
    ch._llamacpp_identity[("127.0.0.1", 8081)] = True
    from src.document_processor import _vision_call_url
    assert _vision_call_url("http://127.0.0.1:8081/v1") == "http://127.0.0.1:8081/v1"
