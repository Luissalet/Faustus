"""Regression tests: OpenAI reasoning models reject a non-default temperature.

o1/o3/o4/gpt-5 only accept the default temperature (1); sending an explicit
value — even 0.0 — returns HTTP 400 "Only the default (1) value is supported".
The OpenAI-compatible payload builders must omit the temperature field for these
models so chat (with a non-default preset) and endpoint probing don't break.
"""
import httpx
import pytest

from src import llm_core


@pytest.mark.parametrize(
    "model",
    ["o1", "o1-mini", "o3", "o3-mini", "o4-mini", "gpt-5", "gpt-5-mini",
     "openrouter/openai/o3-mini", "OpenAI/GPT-5", "kimi-for-coding"],
)
def test_reasoning_models_restrict_temperature(model):
    assert llm_core._restricts_temperature(model) is True


@pytest.mark.parametrize(
    "model",
    ["gpt-4o", "gpt-4.1", "gpt-3.5-turbo", "gpt-4.5-preview",
     "claude-3-5-sonnet", "llama3.1", "", None],
)
def test_normal_models_allow_temperature(model):
    assert llm_core._restricts_temperature(model) is False


def _capture_openai_payload(
    monkeypatch,
    model,
    temperature,
    url="https://api.openai.com/v1/chat/completions",
):
    """Run a synchronous OpenAI-compatible call and return the posted JSON body."""
    llm_core._response_cache.clear()
    seen = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen["json"] = json
        request = httpx.Request("POST", url)
        return httpx.Response(
            200,
            request=request,
            json={"choices": [{"message": {"content": "OK"}}]},
        )

    monkeypatch.setattr(llm_core.httpx, "post", fake_post)
    result = llm_core.llm_call(
        url,
        model,
        [{"role": "user", "content": "Say OK"}],
        temperature=temperature,
        max_tokens=5,
    )
    assert result == "OK"
    return seen["json"]


def test_reasoning_model_payload_omits_temperature(monkeypatch):
    payload = _capture_openai_payload(monkeypatch, "o3-mini", 0.0)
    assert "temperature" not in payload
    # Reasoning models also use max_completion_tokens, which must survive.
    assert payload["max_completion_tokens"] == 5


def test_kimi_for_coding_payload_omits_temperature(monkeypatch):
    payload = _capture_openai_payload(monkeypatch, "kimi-for-coding", 0.1)
    assert "temperature" not in payload
    assert payload["max_tokens"] == 5


def test_normal_model_payload_keeps_temperature(monkeypatch):
    payload = _capture_openai_payload(monkeypatch, "gpt-4o", 0.2)
    assert payload["temperature"] == 0.2
    assert payload["max_tokens"] == 5


def test_normal_model_payload_keeps_temperature_above_one(monkeypatch):
    # OpenAI/local providers may validly use temperatures above 1.0; the clamp
    # is Anthropic-only and must not touch this path.
    payload = _capture_openai_payload(monkeypatch, "gpt-4o", 1.2)
    assert payload["temperature"] == 1.2


def test_local_minimax_mlx_payload_gets_stability_defaults(monkeypatch):
    import src.model_context as model_context

    monkeypatch.setattr(model_context, "is_local_endpoint", lambda _url: True)
    payload = {
        "model": "example-org/MiniMax-M2.7-BF16-mlx-4Bit",
        "temperature": 0.9,
    }

    llm_core._apply_local_generation_stability(
        payload,
        "http://192.168.1.22:8091/v1/chat/completions",
        "example-org/MiniMax-M2.7-BF16-mlx-4Bit",
    )

    assert payload["temperature"] == 0.2
    assert payload["top_p"] == 0.9
    assert payload["top_k"] == 20
    assert payload["max_tokens"] == 2048
    assert payload["repetition_penalty"] == 1.12


def test_chatgpt_subscription_payload_omits_max_output_tokens():
    # ChatGPT Subscription Codex API does not support max_output_tokens —
    # passing it returns HTTP 400 "Unsupported parameter: max_output_tokens".
    # The payload should NOT include max_output_tokens regardless of max_tokens.
    payload = llm_core._build_chatgpt_responses_payload(
        "gpt-5.1-codex",
        [{"role": "user", "content": "Say OK"}],
        temperature=0.2,
        max_tokens=37,
    )

    assert "max_output_tokens" not in payload


def test_chatgpt_subscription_payload_omits_max_output_tokens_when_zero():
    payload = llm_core._build_chatgpt_responses_payload(
        "gpt-5.1-codex",
        [{"role": "user", "content": "Say OK"}],
        temperature=0.2,
        max_tokens=0,
    )

    assert "max_output_tokens" not in payload


# ── generic self-hosted OpenAI-compatible endpoint (llama-server, vLLM, …) ──
#
# Verified live: a llama-server chat turn with none of this arrived at
# /slots with max_tokens=-1, repeat_penalty=1.0, min_p=0 and ran unbounded
# for 15 minutes (7800+ tokens). Unlike Ollama (its own native-endpoint
# floor, src/model_load_options.py) there is no per-model "extra" concept
# for a generic custom endpoint, so the local_repeat_penalty_default/
# local_min_p_default floor applies directly in
# _apply_local_generation_stability, and a local_openai_max_tokens_default
# cap replaces an absent/unbounded max_tokens.

def test_loopback_llama_server_gets_the_sampler_floor_and_output_cap():
    payload = {"model": "qwen3.8-27b-q8-llamacpp", "messages": [], "temperature": 1.0}
    llm_core._apply_local_generation_stability(
        payload, "http://127.0.0.1:8081/v1/chat/completions", "qwen3.8-27b-q8-llamacpp",
    )
    assert payload["repeat_penalty"] == 1.05
    assert payload["min_p"] == 0.05
    assert payload["max_tokens"] == 8192


def test_loopback_llama_server_explicit_max_tokens_is_not_overridden():
    payload = {"model": "qwen3.8-27b-q8-llamacpp", "messages": [], "max_tokens": 512}
    llm_core._apply_local_generation_stability(
        payload, "http://127.0.0.1:8081/v1/chat/completions", "qwen3.8-27b-q8-llamacpp",
    )
    assert payload["max_tokens"] == 512


def test_loopback_llama_server_explicit_overrides_win_over_the_floor():
    """`_apply_gen_overrides_openai` (an explicit saved/per-turn override)
    runs BEFORE `_apply_local_generation_stability` on the real request path
    — the floor here only fills in what is still unset."""
    payload = {"model": "qwen3.8-27b-q8-llamacpp", "messages": [], "repeat_penalty": 1.3, "min_p": 0.2}
    llm_core._apply_local_generation_stability(
        payload, "http://127.0.0.1:8081/v1/chat/completions", "qwen3.8-27b-q8-llamacpp",
    )
    assert payload["repeat_penalty"] == 1.3
    assert payload["min_p"] == 0.2


def test_remote_openai_provider_gets_neither_floor_nor_cap():
    payload = {"model": "gpt-4o", "messages": [], "temperature": 1.0}
    llm_core._apply_local_generation_stability(payload, "https://api.openai.com/v1", "gpt-4o")
    assert "repeat_penalty" not in payload
    assert "min_p" not in payload
    assert "max_tokens" not in payload


def test_local_openai_max_tokens_default_setting_is_registered():
    from src.settings import DEFAULT_SETTINGS
    assert DEFAULT_SETTINGS["local_openai_max_tokens_default"] == 8192


def test_saved_max_tokens_default_setting_is_honoured(monkeypatch):
    from src import settings as settings_mod

    def _fake_get_setting(key, default=None):
        if key == "local_openai_max_tokens_default":
            return 4096
        return default
    monkeypatch.setattr(settings_mod, "get_setting", _fake_get_setting)
    payload = {"model": "qwen3.8-27b-q8-llamacpp", "messages": []}
    llm_core._apply_local_generation_stability(
        payload, "http://127.0.0.1:8081/v1/chat/completions", "qwen3.8-27b-q8-llamacpp",
    )
    assert payload["max_tokens"] == 4096


def test_ollama_endpoint_is_not_routed_through_the_generic_openai_floor():
    """Ollama has its own native-endpoint floor (src/model_load_options.py,
    _apply_gen_overrides_ollama) — the generic self-hosted branch here must
    never double-apply for it."""
    payload = {"model": "qwen3.5:9b", "messages": []}
    llm_core._apply_local_generation_stability(payload, "http://127.0.0.1:11434/v1", "qwen3.5:9b")
    assert "repeat_penalty" not in payload
    assert "min_p" not in payload
    assert "max_tokens" not in payload


# ── the fields actually reach a streamed request body for a loopback endpoint ──

def test_stream_llm_body_carries_the_floor_for_a_loopback_llama_server(monkeypatch):
    """End-to-end through the real streaming call-site order: gen_overrides
    -> cache affinity -> generation stability, exactly as `_stream_llm_inner`
    applies them for the OpenAI-compatible branch."""
    import asyncio
    import json as _json

    class _FakeResp:
        status_code = 200

        async def aiter_lines(self):
            yield "data: " + _json.dumps({"choices": [{"delta": {"content": "ok"}}]})
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
            self.url = ""
            self.payload = {}

        def stream(self, method, url, **kw):
            self.url = url
            self.payload = kw.get("json") or {}
            return _FakeStreamCtx()

    client = _FakeClient()
    monkeypatch.setattr(llm_core, "_get_http_client", lambda: client)
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda u: False)
    monkeypatch.setattr(llm_core, "note_model_activity", lambda *a, **k: None)
    monkeypatch.setattr(llm_core, "_clear_host_dead", lambda *a, **k: None)

    async def run():
        return [c async for c in llm_core.stream_llm(
            "http://127.0.0.1:8081/v1", "qwen3.8-27b-q8-llamacpp",
            [{"role": "user", "content": "hi"}],
        )]

    asyncio.run(run())
    assert client.url.endswith("/v1/chat/completions")
    assert client.payload["repeat_penalty"] == 1.05
    assert client.payload["min_p"] == 0.05
    assert client.payload["max_tokens"] == 8192
