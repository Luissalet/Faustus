"""Per-model load defaults (src/model_load_options.py) reach the Ollama
request body through src/llm_core.py — under explicit per-request overrides.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from src import llm_core
from src import model_load_options as mlo
from src import settings as settings_mod


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", str(tmp_path / "settings.json"))
    settings_mod._invalidate_caches()
    mlo.reset_endpoint_cache()
    # The endpoint the options were saved for: a loopback Ollama on :11434.
    monkeypatch.setattr(mlo, "_endpoint_bases", lambda: {
        "local-ollama": "http://localhost:11434/v1",
        "lan-ollama": "http://192.168.1.20:11434/v1",
    })
    yield
    settings_mod._invalidate_caches()
    mlo.reset_endpoint_cache()


# ── the store ───────────────────────────────────────────────────────────────

def test_sanitize_keeps_the_three_knobs_and_refuses_bad_values():
    assert mlo.sanitize_options({"num_ctx": "8192", "num_gpu": 0, "keep_alive": "10m", "temperature": 0.3}) == \
        {"num_ctx": 8192, "num_gpu": 0, "keep_alive": "10m"}
    assert mlo.sanitize_options({"keep_alive": 600}) == {"keep_alive": 600}
    assert mlo.sanitize_options({"keep_alive": "-1"}) == {"keep_alive": "-1"}
    assert mlo.sanitize_options({"num_ctx": None, "num_gpu": ""}) == {}
    assert mlo.sanitize_options(None) == {}
    for bad in ({"num_ctx": 100}, {"num_ctx": "x"}, {"num_gpu": 5000}, {"keep_alive": "later"}, {"keep_alive": True}, "str"):
        with pytest.raises(ValueError):
            mlo.sanitize_options(bad)


def test_set_get_and_clear(store):
    assert mlo.set_options("local-ollama", "qwen3.5:9b", {"num_ctx": 32768}) == {"num_ctx": 32768}
    assert mlo.get_options("local-ollama", "qwen3.5:9b") == {"num_ctx": 32768}
    assert mlo.options_for_endpoint("local-ollama") == {"qwen3.5:9b": {"num_ctx": 32768}}
    assert mlo.options_for_endpoint("lan-ollama") == {}
    assert settings_mod.load_settings()["model_load_options"] == {"local-ollama|qwen3.5:9b": {"num_ctx": 32768}}
    mlo.set_options("local-ollama", "qwen3.5:9b", {})
    assert mlo.get_options("local-ollama", "qwen3.5:9b") == {}
    assert "local-ollama|qwen3.5:9b" not in settings_mod.load_settings()["model_load_options"]


def test_resolve_matches_the_endpoint_by_host_and_port(store):
    mlo.set_options("local-ollama", "qwen3.5:9b", {"num_ctx": 32768, "keep_alive": "30m"})
    # /v1 and the native /api/chat of the same server are the same endpoint,
    # and every loopback alias is the same host.
    for url in ("http://localhost:11434/v1", "http://127.0.0.1:11434/api/chat", "http://[::1]:11434/v1", "http://0.0.0.0:11434"):
        assert mlo.resolve_for_request(url, "qwen3.5:9b") == {"num_ctx": 32768, "keep_alive": "30m"}, url
    # another model, another server, another port: nothing
    assert mlo.resolve_for_request("http://localhost:11434/v1", "qwen3.5:4b") == {}
    assert mlo.resolve_for_request("http://192.168.1.20:11434/v1", "qwen3.5:9b") == {}
    assert mlo.resolve_for_request("http://localhost:1234/v1", "qwen3.5:9b") == {}
    assert mlo.resolve_for_request("https://api.openai.com/v1", "qwen3.5:9b") == {}
    # the LAN endpoint's own entry resolves for the LAN url only
    mlo.set_options("lan-ollama", "qwen3.5:9b", {"num_gpu": 20})
    assert mlo.resolve_for_request("http://192.168.1.20:11434/v1", "qwen3.5:9b") == {"num_gpu": 20}
    assert mlo.resolve_for_request("http://localhost:11434/v1", "qwen3.5:9b") == {"num_ctx": 32768, "keep_alive": "30m"}


def test_resolve_treats_latest_as_the_bare_name(store):
    mlo.set_options("local-ollama", "nomic-embed-text", {"num_ctx": 2048})
    assert mlo.resolve_for_request("http://localhost:11434/v1", "nomic-embed-text:latest") == {"num_ctx": 2048}


def test_resolve_never_raises(store, monkeypatch):
    mlo.set_options("local-ollama", "qwen3.5:9b", {"num_ctx": 32768})

    def _boom():
        raise RuntimeError("db is gone")

    monkeypatch.setattr(mlo, "_endpoint_bases", _boom)
    assert mlo.resolve_for_request("http://localhost:11434/v1", "qwen3.5:9b") == {}


def test_resolve_skips_the_database_when_nothing_is_saved(store, monkeypatch):
    calls = {"n": 0}

    def _count():
        calls["n"] += 1
        return {}

    monkeypatch.setattr(mlo, "_endpoint_bases", _count)
    assert mlo.resolve_for_request("http://localhost:11434/v1", "qwen3.5:9b") == {}
    assert calls["n"] == 0


def test_the_default_endpoint_id_resolves_against_the_env_ollama(store, monkeypatch):
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
    mlo.set_options(mlo.DEFAULT_ENDPOINT_ID, "qwen3.5:9b", {"num_gpu": 99})
    assert mlo.resolve_for_request("http://localhost:11434/v1", "qwen3.5:9b") == {"num_gpu": 99}


# ── through llm_core to the wire ────────────────────────────────────────────

class _FakeResp:
    status_code = 200

    async def aiter_lines(self):
        yield json.dumps({"message": {"role": "assistant", "content": "ok"}, "done": True})

    async def aread(self):
        return b""


class _FakeStreamCtx:
    async def __aenter__(self):
        return _FakeResp()

    async def __aexit__(self, *a):
        return False


class _FakeClient:
    def __init__(self):
        self.url = ""
        self.payload = {}

    def stream(self, method, url, **kw):
        self.url = url
        self.payload = kw.get("json") or {}
        return _FakeStreamCtx()


def _stream(monkeypatch, url, model, gen_overrides=None, caps=frozenset({"completion", "tools"})):
    client = _FakeClient()
    monkeypatch.setattr(llm_core, "_ollama_model_caps", lambda u, m: caps)
    monkeypatch.setattr(llm_core, "_get_http_client", lambda: client)
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda u: False)
    monkeypatch.setattr(llm_core, "note_model_activity", lambda *a, **k: None)
    monkeypatch.setattr(llm_core, "_clear_host_dead", lambda *a, **k: None)
    monkeypatch.setattr(llm_core, "get_context_length", lambda u, m: 262144)

    async def run():
        return [c async for c in llm_core.stream_llm(url, model, [{"role": "user", "content": "hi"}],
                                                     gen_overrides=gen_overrides)]

    asyncio.run(run())
    return client


def test_saved_num_ctx_reaches_the_ollama_request_body(store, monkeypatch):
    """The whole point: Options… → num_ctx 32768 → `options.num_ctx` on the wire."""
    mlo.set_options("local-ollama", "qwen3.5:9b", {"num_ctx": 32768, "num_gpu": 30, "keep_alive": "30m"})
    client = _stream(monkeypatch, "http://127.0.0.1:11434/v1", "qwen3.5:9b")
    # A saved default is an Ollama `options` knob: the /v1 request moves to the
    # native /api/chat, the only surface that carries it.
    assert client.url.endswith("/api/chat")
    assert client.payload["options"]["num_ctx"] == 32768
    assert client.payload["options"]["num_gpu"] == 30
    assert client.payload["keep_alive"] == "30m"
    assert client.payload["model"] == "qwen3.5:9b"


def test_explicit_overrides_sit_above_the_saved_defaults(store, monkeypatch):
    mlo.set_options("local-ollama", "qwen3.5:9b", {"num_ctx": 32768, "num_gpu": 30})
    client = _stream(monkeypatch, "http://127.0.0.1:11434/v1", "qwen3.5:9b", gen_overrides={"num_ctx": 8192, "top_k": 20})
    assert client.payload["options"]["num_ctx"] == 8192      # /ctx 8192 wins
    assert client.payload["options"]["num_gpu"] == 30        # the default still fills the gap
    assert client.payload["options"]["top_k"] == 20
    # an empty explicit value does not erase the default
    client = _stream(monkeypatch, "http://127.0.0.1:11434/v1", "qwen3.5:9b", gen_overrides={"num_ctx": ""})
    assert client.payload["options"]["num_ctx"] == 32768


def test_no_saved_defaults_leaves_the_request_alone(store, monkeypatch):
    client = _stream(monkeypatch, "http://127.0.0.1:11434/v1", "qwen3.5:9b")
    assert client.url.endswith("/v1/chat/completions")
    assert "options" not in client.payload and "keep_alive" not in client.payload


def test_defaults_for_another_model_do_not_leak(store, monkeypatch):
    mlo.set_options("local-ollama", "qwen3.8:27b-q8_0", {"num_ctx": 16384})
    client = _stream(monkeypatch, "http://127.0.0.1:11434/v1", "qwen3.5:9b")
    assert "options" not in client.payload


def test_non_streaming_async_call_applies_the_defaults(store, monkeypatch):
    mlo.set_options("local-ollama", "qwen3.5:9b", {"num_ctx": 16384, "keep_alive": "1h"})
    captured = {}

    class _R:
        is_success = True
        status_code = 200
        text = ""

        def json(self):
            return {"message": {"role": "assistant", "content": "ok"}}

    async def _post(client, url, headers, json=None, timeout=None, **kw):
        captured["url"] = url
        captured["payload"] = json
        return _R()

    monkeypatch.setattr(llm_core, "_ollama_model_caps", lambda u, m: frozenset({"completion"}))
    monkeypatch.setattr(llm_core, "httpx_post_kimi_aware_async", _post)
    monkeypatch.setattr(llm_core, "_get_http_client", lambda: object())
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda u: False)
    monkeypatch.setattr(llm_core, "note_model_activity", lambda *a, **k: None)
    monkeypatch.setattr(llm_core, "get_context_length", lambda u, m: 262144)
    monkeypatch.setattr(llm_core, "_get_cached_response", lambda k: None)
    out = asyncio.run(llm_core.llm_call_async("http://localhost:11434/v1", "qwen3.5:9b", [{"role": "user", "content": "hi"}]))
    assert out == "ok"
    assert captured["url"].endswith("/api/chat")
    assert captured["payload"]["options"]["num_ctx"] == 16384
    assert captured["payload"]["keep_alive"] == "1h"


def test_keep_alive_is_validated_like_the_other_overrides():
    assert llm_core._clean_gen_overrides({"keep_alive": "10m"}) == {"keep_alive": "10m"}
    assert llm_core._clean_gen_overrides({"keep_alive": 0}) == {"keep_alive": 0}
    assert llm_core._clean_gen_overrides({"keep_alive": "forever"}) == {}
    assert llm_core._clean_gen_overrides({"keep_alive": True}) == {}
    payload = {"model": "m", "messages": []}
    llm_core._apply_gen_overrides_ollama(payload, {"keep_alive": "-1"})
    assert payload["keep_alive"] == "-1" and "options" not in payload


def test_the_chat_route_still_does_not_accept_keep_alive_from_clients():
    """keep_alive is a saved default, not a per-request knob a browser can send."""
    from routes.chat_routes import _parse_gen_overrides
    assert "keep_alive" not in _parse_gen_overrides({"keep_alive": "-1", "num_ctx": 8192})
