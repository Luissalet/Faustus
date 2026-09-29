"""Observed non-streaming usage reaches real traces without changing returns."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from src import context_compactor as cc, llm_core as lc, llm_trace as trace
from tests.test_llm_core_retries import _Response, _wire_sequence


class Response(_Response):
    def __init__(self, data):
        super().__init__(200)
        self.data = data

    def json(self):
        return self.data


def _body(usage=None, *, model="reported-model"):
    data = {"model": model, "choices": [{"message": {"content": "The user requested inspection. No tools were run."}}]}
    if usage is not None:
        data["usage"] = usage
    return data


@pytest.fixture
def traces(tmp_path, monkeypatch):
    monkeypatch.setattr(trace, "_traces_dir", lambda: str(tmp_path))
    monkeypatch.setattr(trace, "tracing_enabled", lambda: True)
    monkeypatch.setattr(lc, "_get_cached_response", lambda *a, **k: None)
    monkeypatch.setattr(cc, "resolve_endpoint", lambda *a, **k: (None, None, None))
    monkeypatch.setattr("src.privacy_policy.assert_outbound", lambda *a, **k: None)
    trace._SEQ_CACHE.clear()
    yield tmp_path
    trace.flush_for_tests()


def _records(directory, session="synthetic"):
    trace.flush_for_tests()
    path = directory / (session + ".jsonl")
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


async def _call(session="synthetic", **kwargs):
    return await lc.llm_call_async("https://synthetic.invalid/v1", "requested-model",
        [{"role": "user", "content": "inspect"}], session_id=session, **kwargs)


def test_real_compactor_session_records_observed_usage_without_cost(traces, monkeypatch):
    calls = _wire_sequence(monkeypatch, [Response(_body({"prompt_tokens": 80, "completion_tokens": 20}))])
    monkeypatch.setattr(cc, "get_context_length", lambda *a: 100)
    monkeypatch.setattr(cc, "compaction_summary_mode", lambda: "llm")
    messages = [{"role": "user" if i % 2 == 0 else "assistant", "content": "synthetic inspection " * 100} for i in range(6)]
    result = asyncio.run(cc.maybe_compact(SimpleNamespace(id="synthetic"),
        "https://synthetic.invalid/v1", "requested-model", messages, persist=False))
    assert result[2] and len(calls) == 1
    rows = _records(traces)
    assert len(rows) == 1
    assert rows[0]["usage"] == {"input_tokens": 80, "output_tokens": 20,
        "model": "reported-model", "requested_model": "requested-model",
        "usage_source": "reported_engine", "cost_state": "unknown"}
    assert rows[0]["session_id"] == "synthetic"


@pytest.mark.parametrize("raw, expected", [
    (None, {}), ({}, {}),
    ({"prompt_tokens": 10}, {"input_tokens": 10}),
    ({"completion_tokens": 0}, {"output_tokens": 0}),
    ({"prompt_tokens": True, "completion_tokens": 4}, {"output_tokens": 4}),
    ({"prompt_tokens": -1}, {}), ({"prompt_tokens": float("nan")}, {}),
    ({"prompt_tokens": float("inf")}, {}), ({"prompt_tokens": "10"}, {}),
    ({"prompt_tokens": 1.5}, {}), ({"prompt_tokens": 2**63}, {}),
    ({"prompt_tokens": 10, "cost": True}, {"input_tokens": 10}),
    ({"prompt_tokens": 10, "cost": 10**400}, {"input_tokens": 10}),
])
def test_missing_partial_and_invalid_dimensions_stay_absent(traces, monkeypatch, raw, expected):
    _wire_sequence(monkeypatch, [Response(_body(raw))])
    assert isinstance(asyncio.run(_call()), str)
    usage = _records(traces)[0]["usage"]
    assert {k: usage[k] for k in ("input_tokens", "output_tokens") if k in usage} == expected
    assert "cost_usd" not in usage
    if expected:
        assert usage["usage_source"] == "reported_engine" and usage["cost_state"] == "unknown"
    else:
        assert usage == {}


def test_reported_cost_and_model_metadata_are_preserved(traces, monkeypatch):
    _wire_sequence(monkeypatch, [Response(_body({"prompt_tokens": 3, "completion_tokens": 2, "cost": 0.01}))])
    text, model = asyncio.run(_call(return_model_metadata=True))
    assert text and model == "reported-model"
    usage = _records(traces)[0]["usage"]
    assert usage["cost_usd"] == 0.01 and usage["cost_source"] == "provider" and usage["cost_state"] == "known"


@pytest.mark.parametrize("bad_field", ["prompt_tokens_details", "completion_tokens_details"])
def test_malformed_extra_counter_does_not_discard_valid_cost(traces, monkeypatch, bad_field):
    key = "cached_tokens" if bad_field == "prompt_tokens_details" else "reasoning_tokens"
    raw = {"prompt_tokens": 10, "cost": 0.01, bad_field: {key: 10**400}}
    _wire_sequence(monkeypatch, [Response(_body(raw))])
    asyncio.run(_call())
    usage = _records(traces)[0]["usage"]
    assert usage["input_tokens"] == 10
    assert usage["cost_usd"] == 0.01 and usage["cost_state"] == "known"
    assert key not in usage


def test_malformed_cost_does_not_discard_valid_cache_counter(traces, monkeypatch):
    raw = {"prompt_tokens": 10, "cost": 10**400, "prompt_tokens_details": {"cached_tokens": 7}}
    _wire_sequence(monkeypatch, [Response(_body(raw))])
    asyncio.run(_call())
    usage = _records(traces)[0]["usage"]
    assert usage["input_tokens"] == 10 and usage["cached_tokens"] == 7
    assert "cost_usd" not in usage and usage["cost_state"] == "unknown"


def test_provider_normalization_preserves_only_observed_counts():
    anthropic = lc._observed_nonstream_usage({"usage": {"input_tokens": 9}}, "anthropic", "claude")
    ollama = lc._observed_nonstream_usage({"prompt_eval_count": 8, "eval_count": 7}, "ollama", "local")
    assert anthropic["input_tokens"] == 9 and "output_tokens" not in anthropic
    assert ollama["input_tokens"] == 8 and ollama["output_tokens"] == 7


@pytest.mark.parametrize("raw", [
    {"total_tokens": 100},
    {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 99},
])
def test_reported_total_is_preserved_without_deriving_or_reconciling(traces, monkeypatch, raw):
    _wire_sequence(monkeypatch, [Response(_body(raw))])
    asyncio.run(_call())
    usage = _records(traces)[0]["usage"]
    assert usage["total_tokens"] == raw["total_tokens"]
    if "prompt_tokens" not in raw:
        assert "input_tokens" not in usage and "output_tokens" not in usage
    else:
        assert usage["input_tokens"] == 2 and usage["output_tokens"] == 3


def test_concurrent_invocations_do_not_share_receipts(traces, monkeypatch):
    _wire_sequence(monkeypatch, [Response(_body({"prompt_tokens": 11})), Response(_body({"completion_tokens": 22}))])
    post = lc.httpx_post_kimi_aware_async
    async def overlap(*a, **k):
        response = await post(*a, **k)
        await asyncio.sleep(0)
        return response
    monkeypatch.setattr(lc, "httpx_post_kimi_aware_async", overlap)
    async def run():
        return await asyncio.gather(_call("first"), _call("second"))
    assert all(asyncio.run(run()))
    first, second = _records(traces, "first")[0]["usage"], _records(traces, "second")[0]["usage"]
    assert first["input_tokens"] == 11 and "output_tokens" not in first
    assert second["output_tokens"] == 22 and "input_tokens" not in second


def test_cached_reply_has_no_fresh_usage_or_provider_call(traces, monkeypatch):
    calls = _wire_sequence(monkeypatch, [Response(_body({"prompt_tokens": 8}))])
    assert asyncio.run(_call())
    monkeypatch.setattr(lc, "_get_cached_response", lambda *a, **k: "cached reply")
    assert asyncio.run(_call("cached")) == "cached reply"
    assert len(calls) == 1
    assert _records(traces, "cached")[0]["usage"] == {}
    assert len(_records(traces)) == 1


def test_schema_failure_keeps_already_received_usage(traces, monkeypatch):
    _wire_sequence(monkeypatch, [Response({"model": "reported-model", "usage": {"prompt_tokens": 4}})])
    with pytest.raises(Exception):
        asyncio.run(_call())
    row = _records(traces)[0]
    assert row["error"] and row["usage"]["input_tokens"] == 4


def test_receipt_callback_failure_cannot_break_reply(traces, monkeypatch):
    _wire_sequence(monkeypatch, [Response(_body({"prompt_tokens": 4}))])
    def broken(usage):
        raise RuntimeError("synthetic receipt failure")
    assert asyncio.run(lc._llm_call_async_impl("https://synthetic.invalid/v1", "requested-model",
        [{"role": "user", "content": "inspect"}], _on_observed_usage=broken))


def test_cancellation_does_not_invent_or_leak_usage(traces, monkeypatch):
    _wire_sequence(monkeypatch, [asyncio.CancelledError(), Response(_body({"prompt_tokens": 6}))])
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_call("cancelled"))
    assert asyncio.run(_call("next"))
    assert _records(traces, "cancelled")[0]["usage"] == {}
    assert _records(traces, "next")[0]["usage"]["input_tokens"] == 6


def test_cancellation_after_receipt_keeps_observed_usage(traces, monkeypatch):
    data = _body({"prompt_tokens": 7})
    data["choices"][0]["message"]["content"] = []
    _wire_sequence(monkeypatch, [Response(data)])
    def cancelled(*a):
        raise asyncio.CancelledError()
    monkeypatch.setattr(lc, "_normalize_mistral_content", cancelled)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_call())
    assert _records(traces)[0]["usage"]["input_tokens"] == 7
