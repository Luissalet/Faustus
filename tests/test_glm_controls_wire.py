"""GLM controls through the actual async/stream HTTP boundary, without inference."""
from copy import deepcopy
import json

import httpx
import pytest

from src import engine_controls, llm_core, provider_reasoning

BASE = "http://10.33.0.9:8012/v1"
MODEL = "glm-5.3-flash-nvfp4"
MESSAGES = [{"role": "user", "content": "Explain the implementation and its tradeoffs"}]


@pytest.fixture
async def wire(monkeypatch):
    class Wire:
        sent = None
        statuses = None
        inject = None

    state = Wire()
    state.sent, state.statuses, state.inject = [], [], {}

    def handler(request):
        payload = json.loads(request.content)
        state.sent.append((str(request.url), deepcopy(payload)))
        status = state.statuses.pop(0) if state.statuses else 200
        if status != 200:
            return httpx.Response(status, json={"error": "reasoning_effort not supported"})
        if payload.get("stream"):
            events = [{"choices": [{"delta": {"content": "ok"}}]},
                      {"choices": [{"delta": {}, "finish_reason": "stop"}]}]
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  text="".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n")
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})

    monkeypatch.setattr(engine_controls, "vllm_thinking_budget", lambda _: True)
    monkeypatch.setattr(llm_core, "_get_cached_response", lambda _: None)
    monkeypatch.setattr(llm_core, "_set_cached_response", lambda *a, **k: None)
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda _: False)
    monkeypatch.setattr(llm_core, "note_model_activity", lambda *a, **k: None)
    monkeypatch.setattr(llm_core, "_clear_host_dead", lambda *a, **k: None)
    monkeypatch.setattr(llm_core, "_apply_local_generation_stability", lambda *a, **k: False)
    monkeypatch.setattr(llm_core, "_local_sampler_default", lambda key, default: 4096 if key == "local_openai_reasoning_budget_default" else default)
    monkeypatch.setattr(llm_core, "_apply_local_cache_affinity", lambda payload, *a: payload.update(deepcopy(state.inject)))
    from src import chat_helpers
    monkeypatch.setattr(chat_helpers, "llamacpp_reasoning_efforts", lambda _: None)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False) as client:
        monkeypatch.setattr(llm_core, "_get_http_client", lambda: client)
        yield state


async def invoke(route, overrides, *, max_retries=0):
    if route == "async":
        answer = await llm_core.llm_call_async(BASE, MODEL, MESSAGES, max_tokens=128,
                                              gen_overrides=overrides, max_retries=max_retries)
        assert answer == "ok"
    else:
        chunks = [chunk async for chunk in llm_core.stream_llm(BASE, MODEL, MESSAGES,
                    max_tokens=128, gen_overrides=overrides, max_retries=max_retries)]
        assert any('"ok"' in chunk for chunk in chunks), chunks
        assert not any('event: error' in chunk for chunk in chunks), chunks


@pytest.mark.parametrize("route", ["async", "stream"])
@pytest.mark.parametrize("think", [False, True])
@pytest.mark.parametrize("budget", [0, -1, 128])
async def test_explicit_budget_and_effort_reach_the_http_wire(wire, route, think, budget):
    await invoke(route, {"think": think, "reasoning_effort": "high", "reasoning_budget": budget})
    assert len(wire.sent) == 1
    url, payload = wire.sent[0]
    assert url == BASE + "/chat/completions"
    assert payload["thinking_token_budget"] == budget
    assert payload["reasoning_effort"] == ("high" if think else "low")
    assert payload["chat_template_kwargs"] == {"enable_thinking": True, "clear_thinking": True}
    assert "reasoning_budget" not in payload and "thinking_budget_tokens" not in payload


@pytest.mark.parametrize("route", ["async", "stream"])
async def test_fast_without_budget_does_not_force_empty_thinking(wire, route):
    await invoke(route, {"think": False, "reasoning_effort": "high"})
    payload = wire.sent[0][1]
    assert payload["reasoning_effort"] == "low"
    assert payload["chat_template_kwargs"]["enable_thinking"] is True
    assert "thinking_token_budget" not in payload


@pytest.mark.parametrize("route", ["async", "stream"])
async def test_native_budget_and_explicit_history_choice_survive_to_wire(wire, route):
    wire.inject = {"thinking_token_budget": 128, "chat_template_kwargs": {"clear_thinking": False}}
    await invoke(route, {"think": False})
    payload = wire.sent[0][1]
    assert payload["thinking_token_budget"] == 128
    assert payload["chat_template_kwargs"]["clear_thinking"] is False
    assert payload["reasoning_effort"] == "low"


async def test_existing_saved_stream_budget_still_applies(wire):
    await invoke("stream", {"think": True, "reasoning_effort": "high"})
    assert wire.sent[0][1]["thinking_token_budget"] == 4096


async def test_async_without_budget_does_not_invent_the_stream_default(wire):
    await invoke("async", {"think": True, "reasoning_effort": "high"})
    assert "thinking_token_budget" not in wire.sent[0][1]


async def test_reasoning_retries_do_not_keep_native_or_nested_stale_fields(wire):
    wire.statuses = [400, 400, 200]
    await invoke("async", {"think": True, "reasoning_effort": "max", "reasoning_budget": 128}, max_retries=2)
    bodies = [payload for _, payload in wire.sent]
    assert [payload.get("reasoning_effort") for payload in bodies] == ["max", "high", None]
    assert bodies[0]["thinking_token_budget"] == bodies[1]["thinking_token_budget"] == 128
    assert "thinking_token_budget" not in bodies[2]
    assert all("reasoning_effort" not in payload["chat_template_kwargs"] for payload in bodies)
    assert bodies[2]["chat_template_kwargs"] == {"enable_thinking": False, "clear_thinking": True}


async def test_stream_transport_retry_preserves_explicit_zero(wire):
    wire.statuses = [503, 200]
    await invoke("stream", {"think": True, "reasoning_effort": "high", "reasoning_budget": 0}, max_retries=2)
    assert len(wire.sent) == 2
    for _, payload in wire.sent:
        assert payload["thinking_token_budget"] == 0
        assert payload["reasoning_effort"] == "high"
        assert payload["chat_template_kwargs"] == {"enable_thinking": True, "clear_thinking": True}


def test_strip_reasoning_removes_old_native_fields_but_preserves_history_choice():
    payload = {"thinking_token_budget": 0, "chat_template_kwargs": {
        "reasoning_effort": "high", "enable_thinking": True, "clear_thinking": False}}
    assert provider_reasoning.strip_reasoning(payload)
    assert payload == {"chat_template_kwargs": {"enable_thinking": False, "clear_thinking": False}}
