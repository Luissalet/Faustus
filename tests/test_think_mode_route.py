# -*- coding: utf-8 -*-
"""Lot T: the chat route's reasoning-mode wiring and the llama-server payload
it produces.

Regression for PENDIENTES "/think on no llega a la petición de llama-server":
the route parsed `gen_overrides` but dropped `reasoning_budget`, so a budget
the client asked for never reached the engine (it silently fell back to the
setting default). The tests below follow a turn from the form fields the
Studio posts to the JSON body llama-server receives.
"""

import asyncio
import json

import pytest

from routes.chat_routes import _parse_gen_overrides, _resolve_think_mode
from src import llm_core

LLAMA = "http://127.0.0.1:8081/v1"
MODEL = "qwen3.8-27b-q8-llamacpp"


# ── _parse_gen_overrides ────────────────────────────────────────────────────

def test_parse_gen_overrides_keeps_reasoning_budget():
    out = _parse_gen_overrides(json.dumps({"think": True, "reasoning_budget": 8192}))
    assert out == {"think": True, "reasoning_budget": 8192}


@pytest.mark.parametrize("bad", [-1, 10 ** 9, "lots", True])
def test_parse_gen_overrides_rejects_bad_budgets(bad):
    assert "reasoning_budget" not in _parse_gen_overrides({"reasoning_budget": bad})


def test_parse_gen_overrides_keeps_think_and_effort():
    out = _parse_gen_overrides({"think": "on", "reasoning_effort": "high"})
    assert out == {"think": True, "reasoning_effort": "high"}


# ── _resolve_think_mode (route precedence) ──────────────────────────────────

@pytest.fixture
def settings(monkeypatch):
    values = {"think_mode_default": "auto", "think_mode_budget_think": 4096, "think_mode_budget_deep": 16384}
    from src import settings as settings_mod

    real = settings_mod.get_setting

    def fake(key, default=None):
        if key in values:
            return values[key]
        return real(key, default)

    monkeypatch.setattr(settings_mod, "get_setting", fake)
    return values


def _resolve(requested, gen, text, **kw):
    kw.setdefault("model", MODEL)
    kw.setdefault("endpoint_url", LLAMA)
    kw.setdefault("chat_mode", "chat")
    kw.setdefault("workspace", None)
    return _resolve_think_mode(requested, gen, text, **kw)


def test_explicit_mode_wins_over_gen_think(settings):
    ov, ev = _resolve("deep", {"think": False}, "hola")
    assert ov == {"think": True, "reasoning_budget": 16384, "reasoning_effort": "high"}
    assert ev["mode"] == "deep" and ev["source"] == "explicit" and ev["budget"] == 16384


def test_gen_think_wins_over_auto(settings):
    ov, ev = _resolve("auto", {"think": True}, "hola")
    assert ov == {"think": True}
    assert ev["source"] == "override" and ev["mode"] == "think"


def test_auto_decides_by_rule(settings):
    ov, ev = _resolve("auto", {}, "hola")
    assert ov == {"think": False}
    assert ev["source"] == "rule" and ev["mode"] == "fast" and ev["requested"] == "auto"
    ov, ev = _resolve("auto", {}, "compara Rust vs Go para un CLI")
    assert ov == {"think": True, "reasoning_budget": 4096}
    assert ev["mode"] == "think"


def test_missing_field_uses_the_default_setting(settings):
    settings["think_mode_default"] = "deep"
    ov, ev = _resolve(None, {}, "hola")
    assert ov["think"] is True and ov["reasoning_budget"] == 16384
    assert ev["requested"] == "deep"


def test_auto_agent_coding_turn_is_never_made_worse(settings):
    ov, ev = _resolve("auto", {}, "mira qué hay aquí", chat_mode="agent", workspace="/tmp/x")
    assert ov["think"] is True
    assert ev["mode"] == "think"


def test_non_thinking_model_is_untouched(settings):
    ov, ev = _resolve("deep", {"top_p": 0.9}, "demuestra algo", model="gpt-4o",
                      endpoint_url="https://api.openai.com/v1")
    assert ov == {"top_p": 0.9} and ev is None


def test_deep_to_a_strict_remote_provider_omits_reasoning_effort(settings):
    ov, _ = _resolve("deep", {}, "x", model="deepseek-reasoner", endpoint_url="https://api.deepseek.com/v1")
    assert "reasoning_effort" not in ov
    assert ov["think"] is True


def test_resolve_think_mode_never_raises(settings, monkeypatch):
    import src.think_mode as tm

    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(tm, "resolve_turn", boom)
    ov, ev = _resolve("deep", {"think": True}, "x")
    assert ov == {"think": True} and ev is None


# ── payload builder regression (llama-server) ───────────────────────────────

class _FakeResp:
    status_code = 200

    async def aiter_lines(self):
        yield "data: " + json.dumps({"choices": [{"delta": {"content": "ok"}}]})
        yield "data: [DONE]"

    async def aread(self):
        return b""


class _FakeStreamCtx:
    async def __aenter__(self):
        return _FakeResp()

    async def __aexit__(self, *a):
        return False


class _FakeClient:
    def __init__(self):
        self.payload = {}

    def stream(self, method, url, **kw):
        self.payload = kw.get("json") or {}
        return _FakeStreamCtx()


@pytest.fixture
def client(monkeypatch):
    c = _FakeClient()
    monkeypatch.setattr(llm_core, "_get_http_client", lambda: c)
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda u: False)
    monkeypatch.setattr(llm_core, "note_model_activity", lambda *a, **k: None)
    monkeypatch.setattr(llm_core, "_clear_host_dead", lambda *a, **k: None)
    return c


def _stream(gen_overrides, text="hola"):
    async def run():
        return [c async for c in llm_core.stream_llm(
            LLAMA, MODEL, [{"role": "user", "content": text}], gen_overrides=gen_overrides,
        )]
    asyncio.run(run())


def test_studio_think_on_reaches_llama_server(client, settings):
    """`/think on` in the Studio posts gen_overrides={"think": true}; the
    llama-server body must carry enable_thinking=true and a budget, even for
    a greeting (the small-talk whitelist must not overrule it)."""
    gen = _parse_gen_overrides(json.dumps({"think": True}))
    gen, _ = _resolve(None, gen, "hola")
    _stream(gen)
    assert client.payload["chat_template_kwargs"] == {"enable_thinking": True}
    assert client.payload["reasoning_budget"] == 4096


def test_client_budget_reaches_llama_server(client, settings):
    """The dropped key: a budget in gen_overrides used to be lost in the
    route and the engine got the 4096 setting default instead."""
    gen = _parse_gen_overrides(json.dumps({"think": True, "reasoning_budget": 12000}))
    gen, _ = _resolve(None, gen, "hola")
    _stream(gen)
    assert client.payload["chat_template_kwargs"] == {"enable_thinking": True}
    assert client.payload["reasoning_budget"] == 12000


def test_deep_mode_reaches_llama_server(client, settings):
    gen, _ = _resolve("deep", {}, "hola")
    _stream(gen)
    assert client.payload["chat_template_kwargs"] == {"enable_thinking": True}
    assert client.payload["reasoning_budget"] == 16384
    assert client.payload["reasoning_effort"] == "high"


def test_fast_mode_reaches_llama_server(client, settings):
    gen, _ = _resolve("fast", {}, "demuestra el teorema de Pitágoras")
    _stream(gen, "demuestra el teorema de Pitágoras")
    assert client.payload["chat_template_kwargs"] == {"enable_thinking": False}
    assert "reasoning_budget" not in client.payload


def test_auto_think_reaches_llama_server(client, settings):
    text = "¿por qué falla este test? AssertionError"
    gen, ev = _resolve("auto", {}, text)
    _stream(gen, text)
    assert ev["mode"] == "think"
    assert client.payload["chat_template_kwargs"] == {"enable_thinking": True}
    assert client.payload["reasoning_budget"] == 4096


# ── agent loop accepts the event ────────────────────────────────────────────

def test_stream_agent_loop_accepts_think_mode():
    import inspect
    from src.agent_loop import stream_agent_loop
    assert "think_mode" in inspect.signature(stream_agent_loop).parameters
