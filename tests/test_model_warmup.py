"""src/model_warmup.py: the default local model is loaded at startup with
keep_alive -1 and re-pinned; remote defaults are left alone."""
import asyncio

import pytest

from src import model_warmup as mw


def test_only_local_ollama_endpoints_are_warmed():
    assert mw._ollama_root("http://127.0.0.1:11434/v1") == "http://127.0.0.1:11434"
    assert mw._ollama_root("http://localhost:11434/api/chat") == "http://localhost:11434"
    assert mw._ollama_root("http://127.0.0.1:8080/v1") == "http://127.0.0.1:8080"   # a local Ollama on another port
    assert mw._ollama_root("https://api.openai.com/v1") is None
    assert mw._ollama_root("https://openrouter.ai/api/v1") is None
    assert mw._keep_alive_value("-1") == -1 and mw._keep_alive_value("2h") == "2h" and mw._keep_alive_value(600) == 600


def test_warm_once_posts_a_promptless_generate_with_keep_alive(monkeypatch):
    calls = []

    class Resp:
        status_code = 200
        text = ""

    class Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            calls.append((url, json))
            return Resp()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", Client)
    monkeypatch.setattr(mw, "resolve_default", lambda: {"url": "http://127.0.0.1:11434/v1", "model": "qwen:27b", "root": "http://127.0.0.1:11434"})
    monkeypatch.setattr(mw, "_settings", lambda: {"enabled": True, "keep_alive": "-1", "every_s": 600.0})
    out = asyncio.run(mw.warm_once())
    assert calls == [("http://127.0.0.1:11434/api/generate", {"model": "qwen:27b", "keep_alive": -1})]
    assert out["ok"] is True and out["model"] == "qwen:27b"


def test_warm_once_without_a_local_default_does_nothing(monkeypatch):
    monkeypatch.setattr(mw, "resolve_default", lambda: None)
    out = asyncio.run(mw.warm_once())
    assert out["ok"] is None and "no local default" in out["detail"]


def test_settings_defaults_exist():
    from src.settings import DEFAULT_SETTINGS
    assert DEFAULT_SETTINGS["warm_default_model"] is True
    assert DEFAULT_SETTINGS["warm_default_model_keep_alive"] == "-1"
