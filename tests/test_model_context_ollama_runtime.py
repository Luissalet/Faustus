"""Ollama context window: prefer the runner's loaded num_ctx (/api/ps) over the
model-card maximum from the known table."""

import src.model_context as mc


class _Resp:
    def __init__(self, payload, ok=True):
        self._payload = payload
        self.is_success = ok

    def json(self):
        return self._payload


def test_model_matching():
    assert mc._ollama_model_matches("qwen3-coder:30b", "qwen3-coder:30b")
    assert mc._ollama_model_matches("qwen3-coder:latest", "qwen3-coder")
    assert not mc._ollama_model_matches("qwen3-coder:30b", "qwen3.8:27b-q8_0")
    assert not mc._ollama_model_matches("qwen3-coder:30b", "qwen3-coder-next:q4_K_M")


def test_loaded_context_wins_over_known_table(monkeypatch):
    mc._ollama_ctx_seen.clear()
    monkeypatch.setattr(mc.httpx, "get", lambda url, timeout=0: _Resp({"models": [
        {"name": "qwen3-coder:30b", "context_length": 32768, "size": 1, "size_vram": 1},
    ]}))
    ctx, known = mc._query_context_length("http://127.0.0.1:11434/v1", "qwen3-coder:30b")
    assert (ctx, known) == (32768, True)
    # Remembered once seen, even when the model is unloaded later.
    monkeypatch.setattr(mc.httpx, "get", lambda url, timeout=0: _Resp({"models": []}))
    monkeypatch.delenv("OLLAMA_CONTEXT_LENGTH", raising=False)
    assert mc._ollama_runtime_context("http://127.0.0.1:11434/v1", "qwen3-coder:30b") == 32768


def test_env_fallback_when_nothing_loaded(monkeypatch):
    mc._ollama_ctx_seen.clear()
    monkeypatch.setattr(mc.httpx, "get", lambda url, timeout=0: _Resp({"models": []}))
    monkeypatch.setenv("OLLAMA_CONTEXT_LENGTH", "65536")
    assert mc._ollama_runtime_context("http://127.0.0.1:11434/v1", "qwen3-coder:30b") == 65536
    monkeypatch.delenv("OLLAMA_CONTEXT_LENGTH", raising=False)
    assert mc._ollama_runtime_context("http://127.0.0.1:11434/v1", "qwen3-coder:30b") is None


def test_non_ollama_local_endpoint_untouched(monkeypatch):
    calls = []

    def _get(url, timeout=0):
        calls.append(url)
        return _Resp({}, ok=False)
    monkeypatch.setattr(mc.httpx, "get", _get)
    mc._query_context_length("http://127.0.0.1:8080/v1", "some-llamacpp-model")
    assert not any("/api/ps" in u for u in calls)
