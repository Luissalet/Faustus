"""Non-stream OpenAI response aliases through real sync/async entry points."""
import asyncio
import copy
import json

import httpx
import pytest

from src import llm_core as core


@pytest.fixture
def call(monkeypatch):
    monkeypatch.setattr(core, "_response_cache", {})
    monkeypatch.setattr(core, "_response_model_cache", {})
    requests, retries = [], []
    reply = {}

    def response(*args, **kwargs):
        requests.append(copy.deepcopy(kwargs["json"]))
        return httpx.Response(200, request=httpx.Request("POST", "https://alias-fixture.invalid/v1/chat/completions"),
                              json={"model": "fixture-served", "choices": [
                                  {"message": reply, "finish_reason": "length"}]})

    async def async_response(*args, **kwargs):
        return response(*args, **kwargs)

    def empty_retry(*args, **kwargs):
        retries.append(1)
        return "Existing empty retry"

    monkeypatch.setattr(core, "httpx_post_kimi_aware", response)
    monkeypatch.setattr(core, "httpx_post_kimi_aware_async", async_response)
    monkeypatch.setattr(core, "_retry_empty_completion", empty_retry)
    monkeypatch.setattr(core, "_get_http_client", lambda: object())

    def invoke(mode, message, *, metadata=False):
        reply.clear()
        reply.update(copy.deepcopy(message))
        args = ("https://alias-fixture.invalid/v1", "fixture-writer",
                [{"role": "user", "content": "Own alias contract fixture."}])
        if mode == "sync":
            return core.llm_call(*args, max_tokens=128)
        return asyncio.run(core.llm_call_async(*args, max_tokens=128, max_retries=1,
                                               return_model_metadata=metadata))

    invoke.requests, invoke.retries = requests, retries
    return invoke


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("content", [None, "", [], [{"type": "image_url", "image_url": {"url": "fixture"}}]])
def test_reasoning_alias_survives_empty_content_on_cold_cache(call, mode, content):
    message = {"content": content, "reasoning": "An admissible final answer."}
    assert call(mode, message) == "An admissible final answer."
    assert len(call.requests) == 1 and call.retries == []
    assert call(mode, message) == "An admissible final answer."
    assert len(call.requests) == 1, "the second identical request must use the existing cache"


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("message,expected", [
    ({"content": "Final content", "reasoning": "New alias reply", "reasoning_content": "Legacy reply"}, "Final content"),
    ({"content": [{"type": "text", "text": "Structured final"}], "reasoning": "New alias reply"}, "Structured final"),
    ({"content": "", "reasoning_content": "Legacy reply", "reasoning": "New alias reply"}, "Legacy reply"),
    ({"content": None, "reasoning_content": "", "reasoning": "New alias reply"}, "New alias reply"),
    ({"content": None, "reasoning_content": "Legacy only reply"}, "Legacy only reply"),
    ({"content": [{"type": "thinking", "thinking": [{"type": "text", "text": "Structured thinking"}]},
                   {"type": "text", "text": "Final text"}], "reasoning": "New alias reply"},
     "Structured thinking\n\nFinal text"),
])
def test_content_legacy_and_mistral_precedence_are_preserved(call, mode, message, expected):
    assert call(mode, message) == expected
    assert call.retries == [] and len(call.requests) == 1


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("value", [None, "", "   ", "???!!!", "/umd``", "yes"])
def test_reasoning_alias_uses_the_existing_filter(call, mode, value):
    result = call(mode, {"content": "", "reasoning": value})
    if mode == "sync":
        assert result == "Existing empty retry" and call.retries == [1]
    else:
        assert result == "" and call.retries == []
        assert core._response_cache == {} and core._response_model_cache == {}
    assert len(call.requests) == 1


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_truthy_legacy_field_retains_precedence_even_when_filter_rejects_it(call, mode):
    result = call(mode, {"content": "", "reasoning_content": "???", "reasoning": "New admissible answer"})
    assert result == ("Existing empty retry" if mode == "sync" else "")


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_truncated_json_in_reasoning_is_not_repaired(call, mode):
    unfinished = '{"summary": "Own unfinished reasoning reply", "findings": ['
    result = call(mode, {"content": "", "reasoning": unfinished})
    assert result == unfinished
    with pytest.raises(json.JSONDecodeError):
        json.loads(result)
    assert call.retries == [] and len(call.requests) == 1


def test_async_actual_model_metadata_and_cache_remain_paired(call):
    message = {"content": None, "reasoning": "Admissible cached answer."}
    assert call("async", message, metadata=True) == ("Admissible cached answer.", "fixture-served")
    assert call("async", message, metadata=True) == ("Admissible cached answer.", "fixture-served")
    assert len(call.requests) == 1 and call.retries == []


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_response_alias_does_not_change_request_payload(call, mode):
    assert call(mode, {"content": None, "reasoning_content": "Same fixture reply."}) == "Same fixture reply."
    core._response_cache.clear()
    core._response_model_cache.clear()
    assert call(mode, {"content": None, "reasoning": "Same fixture reply."}) == "Same fixture reply."
    assert len(call.requests) == 2 and call.requests[0] == call.requests[1]
    assert call.retries == []
