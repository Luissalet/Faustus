"""Per-call HTTP evidence without model text or changes to completion behavior."""
import asyncio
import copy
import json
import sys
import types
from types import SimpleNamespace

import httpx
import pytest

from src import llm_core as core


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setattr(core, "_response_cache", {})
    monkeypatch.setattr(core, "_response_model_cache", {})
    requests, observed = [], []
    data = {"id": "own-http-id", "model": "served-fixture", "usage": {
        "prompt_tokens": 9, "completion_tokens": 12, "total_tokens": 21,
        "completion_tokens_details": {"reasoning_tokens": 11}}, "choices": [{
        "index": 0, "finish_reason": "length", "message": {
            "content": None, "reasoning": "Private reasoning 日本語.", "thinking": "secret thinking"}}]}
    status = [200]
    async def post(*args, **kwargs):
        requests.append(copy.deepcopy(kwargs["json"]))
        code = status.pop(0) if len(status) > 1 else status[0]
        body = ({"error": {"message": "min_p and logit_bias sampling parameters are not yet supported with speculative decoding"}}
                if code == 400 and len(status) == 1 and status[0] == 200 else copy.deepcopy(data))
        return httpx.Response(code, request=httpx.Request("POST", "https://evidence.invalid/v1/chat/completions"),
                              json=body, headers={"authorization": "PRIVATE_HEADER"})
    monkeypatch.setattr(core, "_get_http_client", lambda: SimpleNamespace(post=post))
    def invoke(observer=observed.append, **kwargs):
        return asyncio.run(core.llm_call_async("https://evidence.invalid/v1", "asked-fixture",
            [{"role": "user", "content": "PRIVATE_PROMPT"}], max_tokens=1200, max_retries=1,
            _response_observer=observer, **kwargs))
    return invoke, data, requests, observed, status


def test_http_evidence_keeps_finality_usage_channel_shapes_and_no_text(provider):
    invoke, data, requests, evidence, _ = provider
    assert invoke(return_model_metadata=True) == ("Private reasoning 日本語.", "served-fixture")
    assert len(evidence) == len(requests) == 1
    receipt = evidence[0]
    assert receipt["source"] == "http_response" and receipt["http_status"] == 200 and receipt["attempt"] == 1
    assert receipt["request"]["controls"]["max_tokens"] == requests[0]["max_tokens"] == 1200
    response = receipt["response"]
    assert response["model"] == "served-fixture" and response["id"] == "own-http-id"
    assert response["usage"]["input_tokens"] == 9 and response["usage"]["output_tokens"] == 12
    assert response["usage"]["reasoning_tokens"] == 11
    choice = response["choices"][0]
    assert choice["finish_reason"] == "length"
    channels = choice["channels"]
    assert channels["content"] == {"present": True, "type": "NoneType"}
    assert channels["reasoning_content"] == {"present": False}
    value = data["choices"][0]["message"]["reasoning"]
    assert channels["reasoning"]["chars"] == len(value)
    assert channels["reasoning"]["utf8_bytes"] == len(value.encode("utf-8"))
    encoded = json.dumps(receipt, ensure_ascii=False)
    for secret in ("Private reasoning", "secret thinking", "PRIVATE_HEADER", "PRIVATE_PROMPT"):
        assert secret not in encoded


def test_final_payload_is_observed_without_altering_it(provider, monkeypatch):
    invoke, _, requests, evidence, _ = provider
    def final_controls(payload, *args, **kwargs):
        payload.update(max_tokens=1777, reasoning_effort="low", thinking_token_budget=33,
            chat_template_kwargs={"enable_thinking": True, "clear_thinking": False},
            response_format={"type": "json_schema", "json_schema": {"strict": True,
                "schema": {"PRIVATE_SCHEMA": "must not appear"}}})
    monkeypatch.setattr(core, "_apply_verified_engine_controls", final_controls)
    invoke()
    assert evidence[0]["request"]["controls"]["max_tokens"] == requests[0]["max_tokens"] == 1777
    assert evidence[0]["request"]["chat_template_kwargs"] == requests[0]["chat_template_kwargs"]
    assert evidence[0]["request"]["response_format"] == {"type": "json_schema", "strict": True}
    assert "PRIVATE_SCHEMA" not in json.dumps(evidence)


def test_callback_error_does_not_change_the_answer_or_retry(provider):
    invoke, _, requests, _, _ = provider
    def broken(receipt):
        raise RuntimeError("private observer failed")
    assert invoke(broken) == "Private reasoning 日本語."
    assert len(requests) == 1


def test_cached_reply_has_no_new_http_evidence(provider):
    invoke, _, requests, evidence, _ = provider
    assert invoke() == invoke() == "Private reasoning 日本語."
    assert len(requests) == len(evidence) == 1


@pytest.mark.parametrize("content", ["", "   ", [], [{"type": "text", "text": "Private structured content"}]])
def test_shapes_do_not_promote_a_channel_to_json_or_leak_structured_text(provider, content):
    invoke, data, _, evidence, _ = provider
    data["choices"][0]["message"]["content"] = content
    invoke()
    shape = evidence[0]["response"]["choices"][0]["channels"]["content"]
    assert shape["present"] is True and shape["type"] == type(content).__name__
    if isinstance(content, str):
        assert shape["nonblank_chars"] == 0
    else:
        assert shape["items"] == len(content)
    assert "Private structured content" not in json.dumps(evidence)


def test_http_error_is_observed_before_the_existing_exception(provider):
    invoke, data, requests, evidence, status = provider
    status[0] = 400
    data.clear(); data.update(error={"message": "PRIVATE_ERROR_BODY"})
    with pytest.raises(Exception):
        invoke()
    assert len(requests) == len(evidence) == 1
    assert evidence[0]["http_status"] == 400
    assert "PRIVATE_ERROR_BODY" not in json.dumps(evidence)


def test_automatic_sampler_compatibility_keeps_every_physical_http_reply(provider, monkeypatch):
    invoke, _, requests, evidence, status = provider
    status[:] = [400, 200]
    monkeypatch.setattr(core, "_MIN_P_REJECTED_HOSTS", set())
    monkeypatch.setattr(core, "_is_self_hosted_openai_compatible", lambda url: True)
    def automatic(payload, *args, **kwargs):
        payload["min_p"] = 0.05
        return True
    monkeypatch.setattr(core, "_apply_local_generation_stability", automatic)
    assert invoke() == "Private reasoning 日本語."
    assert [r["http_status"] for r in evidence] == [400, 200]
    assert [r["http_response_index"] for r in evidence] == [1, 2]
    assert [r["attempt"] for r in evidence] == [1, 1]
    assert [r["request"]["controls"]["min_p"] for r in evidence] == [0.05, 0.0]
    assert [r["min_p"] for r in requests] == [0.05, 0.0]


def test_observer_does_not_add_request_fields_or_model_calls(provider):
    invoke, _, requests, evidence, _ = provider
    invoke(None)
    core._response_cache.clear(); core._response_model_cache.clear()
    invoke()
    assert requests[0] == requests[1] and len(requests) == 2 and len(evidence) == 1


def test_cli_result_does_not_invent_http_evidence(monkeypatch):
    module = types.ModuleType("src.cli_model")
    async def complete(*args):
        return "Own CPU CLI fixture"
    module.complete_async = complete
    monkeypatch.setitem(sys.modules, "src.cli_model", module)
    evidence = []
    result = asyncio.run(core.llm_call_async("faustus-cli://fixture", "fixture", [],
        return_model_metadata=True, _response_observer=evidence.append))
    assert result == ("Own CPU CLI fixture", "fixture") and evidence == []


def test_invalid_json_records_shape_before_existing_parser_error(provider, monkeypatch):
    invoke, _, _, evidence, _ = provider
    async def post(*args, **kwargs):
        return httpx.Response(200, request=httpx.Request("POST", "https://evidence.invalid/v1"),
                              content=b"PRIVATE_INVALID_JSON")
    monkeypatch.setattr(core, "_get_http_client", lambda: SimpleNamespace(post=post))
    with pytest.raises(json.JSONDecodeError):
        invoke()
    assert evidence[0]["response"] == {"json_error_type": "JSONDecodeError", "body_bytes": 20}
    assert "PRIVATE_INVALID_JSON" not in json.dumps(evidence)
