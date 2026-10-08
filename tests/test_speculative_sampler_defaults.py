"""Automatic sampler compatibility never drops explicit parameters or partial output."""
import asyncio
import copy
import json

import httpx
import pytest

from src import llm_core as core

BASE_URL = "http://192.168.50.97:19097/v1"
URL = BASE_URL + "/chat/completions"
ERROR = "The min_p and logit_bias sampling parameters are not yet supported with speculative decoding."


@pytest.fixture(autouse=True)
def isolated_sampler(monkeypatch):
    monkeypatch.setattr(core, "_is_self_hosted_openai_compatible", lambda url: url in (BASE_URL, URL))
    monkeypatch.setattr(core, "_is_local_ollama_target", lambda url: False)
    monkeypatch.setattr(core, "_local_sampler_default", lambda key, default: default)
    monkeypatch.setattr(core, "_local_sampler_default_optional", lambda key: None)
    monkeypatch.setattr(core, "_model_load_defaults", lambda *args: {})
    monkeypatch.setattr(core, "_is_host_dead", lambda url: False)
    monkeypatch.setattr(core, "_clear_host_dead", lambda *args: None)
    monkeypatch.setattr(core, "note_model_activity", lambda *args: None)
    monkeypatch.setattr(core, "_fit_reasoning_effort_to_template", lambda *args: None)


def test_stability_tracks_only_its_own_default():
    payload = {}
    assert core._apply_local_generation_stability(payload, URL, "test-model")
    assert payload["min_p"] == .05
    payload = {"min_p": .2}
    assert not core._apply_local_generation_stability(payload, URL, "test-model")
    assert payload["min_p"] == .2


@pytest.mark.parametrize("automatic,partial,status,url,extra,error", [
    (False, False, 400, URL, {}, ERROR),
    (True, True, 400, URL, {}, ERROR),
    (True, False, 500, URL, {}, ERROR),
    (True, False, 400, "https://api.example.test/v1", {}, ERROR),
    (True, False, 400, URL, {"logit_bias": {}}, ERROR),
    (True, False, 400, URL, {}, "Invalid request"),
    (True, False, 400, URL, {"min_p": 0}, ERROR),
])
def test_unproven_or_explicit_rejections_are_not_negotiated(automatic, partial, status, url, extra, error):
    payload = {"min_p": .05, "reasoning_effort": "xhigh", **extra}
    before = copy.deepcopy(payload)
    assert not core._neutralize_rejected_automatic_min_p(payload, url, status, error,
                                                       automatic=automatic, partial=partial)
    assert payload == before


def test_sync_completion_retries_once_without_changing_reasoning(monkeypatch):
    seen = []
    def post(url, **kwargs):
        seen.append(copy.deepcopy(kwargs["json"]))
        status = 400 if len(seen) == 1 else 200
        return httpx.Response(status, json={"error": {"message": ERROR}} if status == 400 else {"ok": True})
    monkeypatch.setattr(core.httpx, "post", post)
    payload = {"min_p": .05, "reasoning_effort": "xhigh", "max_tokens": 8192}
    assert core.httpx_post_kimi_aware(URL, {}, json=payload, _automatic_min_p=True).status_code == 200
    assert seen == [{"min_p": .05, "reasoning_effort": "xhigh", "max_tokens": 8192},
                    {"min_p": 0., "reasoning_effort": "xhigh", "max_tokens": 8192}]


@pytest.mark.asyncio
async def test_async_completion_correction_is_bounded():
    class Client:
        calls = []
        async def post(self, url, **kwargs):
            self.calls.append(copy.deepcopy(kwargs["json"]))
            return httpx.Response(400, json={"error": {"message": ERROR}})
    client = Client()
    result = await core.httpx_post_kimi_aware_async(client, URL, {}, json={"min_p": .05}, _automatic_min_p=True)
    assert result.status_code == 400
    assert len(client.calls) == 2
    assert client.calls[1]["min_p"] == 0


class Response:
    def __init__(self, lines=(), status=200):
        self.lines, self.status_code, self.headers = lines, status, {}
        self.closed = False
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        return False
    async def aread(self):
        return json.dumps({"error": {"message": ERROR}}).encode()
    async def aclose(self):
        self.closed = True
    async def aiter_lines(self):
        for line in self.lines:
            yield line


ERR_LINE = "data: " + json.dumps({"error": {"message": ERROR, "code": 400}})
OK_LINE = 'data: {"choices":[{"delta":{"content":"answer"},"finish_reason":"stop"}]}'


def stream(monkeypatch, responses, overrides=None):
    class Client:
        calls = []
        def stream(self, method, url, **kwargs):
            if self.calls:
                assert responses[len(self.calls) - 1].closed
            self.calls.append(copy.deepcopy(kwargs["json"]))
            return responses[len(self.calls) - 1]
    client = Client()
    monkeypatch.setattr(core, "_get_http_client", lambda: client)
    async def drive():
        return [chunk async for chunk in core._stream_llm_inner(BASE_URL, "test-model",
            [{"role": "user", "content": "Explain context verification"}], gen_overrides=overrides, max_retries=0)]
    return client, asyncio.run(drive())


@pytest.mark.parametrize("rejected", [Response(status=400), Response([ERR_LINE])])
def test_stream_negotiates_http_and_sse_before_output(monkeypatch, rejected):
    client, chunks = stream(monkeypatch, [rejected, Response([OK_LINE, "data: [DONE]"])], {"reasoning_effort": "xhigh"})
    assert len(client.calls) == 2
    first, second = client.calls
    assert first["min_p"] == .05 and second["min_p"] == 0
    assert first["reasoning_effort"] == second["reasoning_effort"] == "xhigh"
    assert first["model"] == second["model"] and first["messages"] == second["messages"]
    assert any('"delta": "answer"' in chunk for chunk in chunks)
    assert not any(chunk.startswith("event: error") for chunk in chunks)


def test_stream_explicit_min_p_fails_without_retry(monkeypatch):
    client, chunks = stream(monkeypatch, [Response([ERR_LINE])], {"min_p": .15})
    assert len(client.calls) == 1 and client.calls[0]["min_p"] == .15
    assert any(chunk.startswith("event: error") for chunk in chunks)


@pytest.mark.parametrize("first_delta", [OK_LINE, 'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call-1","function":{"name":"example","arguments":"{}"}}]}}]}'])
def test_stream_partial_content_or_tools_are_never_replayed(monkeypatch, first_delta):
    client, chunks = stream(monkeypatch, [Response([first_delta, ERR_LINE])])
    assert len(client.calls) == 1
    assert any(chunk.startswith("event: error") for chunk in chunks)


def test_stream_repeated_rejection_has_no_third_attempt(monkeypatch):
    client, chunks = stream(monkeypatch, [Response([ERR_LINE]), Response([ERR_LINE])])
    assert len(client.calls) == 2
    assert any(chunk.startswith("event: error") for chunk in chunks)


def test_stream_retry_stays_bounded_if_override_is_lost(monkeypatch):
    monkeypatch.setattr(core, "_apply_gen_overrides_openai", lambda *args: None)
    client, chunks = stream(monkeypatch, [Response([ERR_LINE]), Response([ERR_LINE])])
    assert len(client.calls) == 2
    assert all(call["min_p"] == .05 for call in client.calls)
    assert any(chunk.startswith("event: error") for chunk in chunks)


def test_explicit_samplers_reach_configured_lan_servers():
    overrides = {"min_p": .15, "top_k": 11, "repeat_penalty": 1.2}
    payload = {}
    core._apply_gen_overrides_openai(payload, overrides, BASE_URL)
    assert payload == overrides
    hosted = {}
    core._apply_gen_overrides_openai(hosted, overrides, "https://api.example.test/v1")
    assert hosted == {}
