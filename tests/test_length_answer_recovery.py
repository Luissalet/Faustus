"""Requested unlimited output must reach the endpoint without an implicit 8k cap."""
import asyncio
import json

import httpx
import pytest

from src import llm_core as core

URL = "http://127.0.0.1:18002/v1"
MODEL = "glm-5.3-flash-nvfp4"
ANSWER = "Answer from the same endpoint after its requested reasoning."


@pytest.fixture
def native_payload(monkeypatch):
    monkeypatch.setattr(core, "_is_self_hosted_openai_compatible", lambda _url: True)
    monkeypatch.setattr(core, "_is_local_ollama_target", lambda _url: False)
    monkeypatch.setattr(core, "_local_sampler_default", lambda _key, default: default)
    monkeypatch.setattr(core, "_local_sampler_default_optional", lambda _key: None)
    core._response_cache.clear()


@pytest.mark.parametrize("requested", [0, 512])
def test_sync_wire_preserves_requested_output_budget(monkeypatch, native_payload, requested):
    seen = []
    def post(url, **kwargs):
        seen.append(kwargs["json"])
        return httpx.Response(200, request=httpx.Request("POST", url),
            json={"choices": [{"message": {"content": ANSWER}}]})
    monkeypatch.setattr(core.httpx, "post", post)
    assert core.llm_call(URL, MODEL, [{"role": "user", "content": "Explain the evidence"}], max_tokens=requested) == ANSWER
    assert seen[-1].get("max_tokens") == (512 if requested else None)


def test_async_completion_wire_preserves_zero_and_requested_reasoning(monkeypatch, native_payload):
    seen = []
    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": ANSWER}}]})
    async def drive():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            monkeypatch.setattr(core, "_get_http_client", lambda: client)
            return await core.llm_call_async(URL, MODEL, [{"role": "user", "content": "Explain the evidence"}],
                max_tokens=0, gen_overrides={"think": True, "reasoning_budget": 16384}, max_retries=0)
    assert asyncio.run(drive()) == ANSWER
    assert "max_tokens" not in seen[-1]
    assert seen[-1]["chat_template_kwargs"]["enable_thinking"] is True
    assert seen[-1]["reasoning_budget"] == 16384


def test_stream_can_finish_after_16k_reasoning_without_an_injected_8k_cap(monkeypatch, native_payload):
    seen = []
    def handler(request):
        payload = json.loads(request.content); seen.append(payload)
        # Simulates the observed endpoint: an injected cap below the requested
        # reasoning ends with length and no answer. No cap allows its answer.
        exhausted = 0 < payload.get("max_tokens", 0) <= 16384
        delta = {"reasoning_content": "The requested reasoning exhausted the injected cap."} if exhausted else {"content": ANSWER}
        event = {"choices": [{"delta": delta, "finish_reason": "length" if exhausted else "stop"}]}
        return httpx.Response(200, text="data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n", headers={"content-type": "text/event-stream"})
    async def drive():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            monkeypatch.setattr(core, "_get_http_client", lambda: client)
            return [chunk async for chunk in core._stream_llm_inner(URL, MODEL,
                [{"role": "user", "content": "Explain the gathered evidence"}], max_tokens=0,
                gen_overrides={"think": True, "reasoning_budget": 16384},
                tools=[{"type": "function", "function": {"name": "web_search", "parameters": {"type": "object"}}}], max_retries=0)]
    chunks = asyncio.run(drive())
    assert any(ANSWER in chunk for chunk in chunks)
    assert "max_tokens" not in seen[-1]
    assert seen[-1]["chat_template_kwargs"]["enable_thinking"] is True
    assert seen[-1]["reasoning_budget"] == 16384
    assert seen[-1]["tools"][0]["function"]["name"] == "web_search"
