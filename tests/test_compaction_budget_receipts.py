"""Compactor receipts charge the captured turn ledger before inference admission."""
import asyncio
import json
from contextlib import asynccontextmanager

import pytest

from src import agent_loop as al, autonomy_budget as ab, context_compactor as cc, llm_core as lc
from tests.test_autonomy_budget import _patch_common, _collect, _events
from tests.test_llm_core_retries import _Response, _wire_sequence


class Response(_Response):
    def __init__(self, usage=None, *, bad_schema=False):
        super().__init__(200)
        self.data = {"model": "utility"}
        if not bad_schema:
            self.data["choices"] = [{"message": {"content": "The user requested inspection. No tools were run."}}]
        if usage is not None:
            self.data["usage"] = usage

    def json(self):
        return self.data


@pytest.fixture
def root(monkeypatch):
    _patch_common(monkeypatch)
    ledger = ab.Ledger()
    monkeypatch.setattr(ab, "Ledger", lambda: ledger)
    monkeypatch.setattr(ab, "resolve_budget", lambda *a, **k: ab.Budget(max_tokens=100))
    monkeypatch.setattr(al, "compact_with_integrity", lambda *a, **k: (a[0], None))
    monkeypatch.setattr(cc, "get_context_length", lambda *a: 100)
    monkeypatch.setattr(cc, "compaction_summary_mode", lambda: "llm")
    monkeypatch.setattr(cc, "resolve_endpoint", lambda *a, **k: (None, None, None))
    monkeypatch.setattr("src.privacy_policy.assert_outbound", lambda *a, **k: None)
    monkeypatch.setattr(lc, "_get_cached_response", lambda *a, **k: None)
    @asynccontextmanager
    async def slot(*a, **k):
        yield
    monkeypatch.setattr(lc, "_local_model_slot", slot)
    return ledger


def _run(**kwargs):
    messages = [{"role": "user" if i % 2 == 0 else "assistant",
                 "content": "synthetic inspection " * 100} for i in range(6)]
    endpoint = kwargs.pop("endpoint_url", "https://synthetic.invalid/v1")
    return _events(_collect(al.stream_agent_loop(
        endpoint, "m", messages,
        defer_context_shaping=True, max_rounds=1, owner="admin", **kwargs)))


def test_initial_remote_compaction_charges_and_stops_before_main(root, monkeypatch):
    calls = _wire_sequence(monkeypatch, [Response({"prompt_tokens": 80, "completion_tokens": 20}) for _ in range(20)])
    async def forbidden(*a, **k):
        pytest.fail("budget must stop before main inference")
        yield
    monkeypatch.setattr(lc, "stream_llm", forbidden)
    events = _run()
    assert calls and root.tokens == len(calls) * 100
    stop = next(e for e in events if e.get("type") == "budget_exhausted")
    assert stop.get("data", stop)["kind"] == "tokens"
    metrics = next(e["data"] for e in events if e.get("type") == "metrics")
    assert sum(r["charged_tokens"] for r in metrics["compaction_usage"]) == root.tokens
    assert all(r["usage_source"] == "reported_engine" for r in metrics["compaction_usage"])


@pytest.mark.parametrize("statuses", [None, {503}])
def test_fallback_compaction_stops_without_provider_error_or_next_candidate(root, monkeypatch, statuses):
    # Initial prep is unmetered by this response; fallback prep reports enough
    # usage to exhaust the same budget, after the round's initial check.
    calls = _wire_sequence(monkeypatch, [Response(), Response()] + [Response({"total_tokens": 100}) for _ in range(20)])
    streams = []
    async def primary_failure(url, *a, **k):
        streams.append(url)
        assert len(streams) == 1, "no fallback inference may start after compaction exhausts budget"
        yield 'event: error\ndata: {"status":503,"error":"synthetic unavailable"}\n\n'
    monkeypatch.setattr(lc, "stream_llm", primary_failure)
    events = _run(fallbacks=[("https://fallback.invalid/v1", "fallback", None),
                            ("https://third.invalid/v1", "third", None)], fallback_statuses=statuses)
    assert len(calls) == 3 and root.tokens == 100
    assert streams == ["https://synthetic.invalid/v1"]
    assert any(e.get("type") == "budget_exhausted" for e in events)
    assert not any("preparation failed" in str(e).lower() for e in events)


@pytest.mark.parametrize("usage", [{"total_tokens": 1000}, {"cost": 0.001}])
def test_local_utility_is_excluded_even_with_remote_main(root, monkeypatch, usage):
    monkeypatch.setattr(cc, "resolve_endpoint", lambda *a, **k: ("http://127.0.0.1:8080/v1", "utility", None))
    _wire_sequence(monkeypatch, [Response(usage) for _ in range(20)])
    admitted = []
    async def stop(*a, **k):
        admitted.append(True)
        if False:
            yield
        raise RuntimeError("STOP_MAIN")
    monkeypatch.setattr(lc, "stream_llm", stop)
    with pytest.raises(RuntimeError, match="STOP_MAIN"):
        _run()
    assert admitted and root.tokens == 0 and root.remote_spend == 0


@pytest.mark.parametrize("usage, expected, lower_bound", [
    ({"total_tokens": 100, "prompt_tokens": 2, "completion_tokens": 3}, 100, False),
    ({"prompt_tokens": 100}, 100, True),
    ({"prompt_tokens": True, "completion_tokens": 100}, 100, True),
])
def test_reported_total_preferred_and_known_dimensions_lower_bound(root, monkeypatch, usage, expected, lower_bound):
    _wire_sequence(monkeypatch, [Response(usage) for _ in range(20)])
    events = _run()
    metrics = next(e["data"] for e in events if e.get("type") == "metrics")
    receipts = metrics["compaction_usage"]
    assert root.tokens == expected * len(receipts)
    assert all(r["tokens_lower_bound"] is lower_bound for r in receipts)


def test_schema_error_after_provider_receipt_still_charges(root, monkeypatch):
    calls = _wire_sequence(monkeypatch, [Response({"total_tokens": 100}, bad_schema=True) for _ in range(20)])
    events = _run()
    assert root.tokens == len(calls) * 100
    assert any(e.get("type") == "budget_exhausted" for e in events)


def test_cached_summary_does_not_charge(root, monkeypatch):
    calls = _wire_sequence(monkeypatch, [])
    monkeypatch.setattr(lc, "_get_cached_response", lambda *a, **k: "The user requested inspection. No tools were run.")
    async def stop(*a, **k):
        if False:
            yield
        raise RuntimeError("STOP_MAIN")
    monkeypatch.setattr(lc, "stream_llm", stop)
    with pytest.raises(RuntimeError, match="STOP_MAIN"):
        _run()
    assert not calls and root.tokens == 0


def test_callback_failure_is_nonfatal_and_receipt_context_is_private(root, monkeypatch):
    _wire_sequence(monkeypatch, [Response({"total_tokens": 3})])
    seen = []
    def broken(usage, *, endpoint_local):
        seen.append((usage, endpoint_local))
        raise RuntimeError("synthetic observer failure")
    text = asyncio.run(cc.summarize_rows([{"role": "user", "content": "inspect"}],
        endpoint_url="https://synthetic.invalid/v1", model="m", _usage_observer=broken))
    assert text and len(seen) == 1 and seen[0][1] is False
    assert not any("url" in key for key in seen[0][0])


@pytest.mark.parametrize("after_receipt", [False, True])
def test_root_cancellation_charges_only_already_observed_receipt(root, monkeypatch, after_receipt):
    response = Response({"total_tokens": 100})
    if after_receipt:
        response.data["choices"][0]["message"]["content"] = []
        def cancel(*a):
            raise asyncio.CancelledError()
        monkeypatch.setattr(lc, "_normalize_mistral_content", cancel)
        _wire_sequence(monkeypatch, [response])
    else:
        _wire_sequence(monkeypatch, [asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        _run()
    assert root.tokens == (100 if after_receipt else 0)


def test_local_main_policy_does_not_gain_caps_from_remote_utility(root, monkeypatch):
    monkeypatch.setattr(cc, "resolve_endpoint", lambda *a, **k: ("https://utility.invalid/v1", "utility", None))
    calls = _wire_sequence(monkeypatch, [Response({"total_tokens": 100}) for _ in range(20)])
    async def stop(*a, **k):
        if False:
            yield
        raise RuntimeError("STOP_MAIN")
    monkeypatch.setattr(lc, "stream_llm", stop)
    with pytest.raises(RuntimeError, match="STOP_MAIN"):
        _run(endpoint_url="http://127.0.0.1:8080/v1")
    # Local-main setup can make other auxiliary probes; this observer charges
    # compaction only. The remote receipt must not create a new local cap.
    assert calls and root.tokens >= 100


def test_main_stream_tokens_and_compaction_receipts_remain_separate(root, monkeypatch):
    _wire_sequence(monkeypatch, [Response({"total_tokens": 5}) for _ in range(20)])
    async def stream(*a, **k):
        yield 'data: ' + json.dumps({"delta": '```bash\necho synthetic\n```'}) + '\n\n'
        yield 'data: ' + json.dumps({"type": "usage", "data": {"input_tokens": 20, "output_tokens": 10}}) + '\n\n'
        yield 'data: [DONE]\n\n'
    monkeypatch.setattr(lc, "stream_llm", stream)
    events = _run(relevant_tools={"bash"})
    metrics = next(e["data"] for e in events if e.get("type") == "metrics")
    auxiliary = sum(r["charged_tokens"] for r in metrics["compaction_usage"])
    assert root.tokens > auxiliary
    assert (root.tokens - auxiliary) % 30 == 0


def test_cost_only_receipt_exhausts_remote_spend_without_inventing_tokens(root, monkeypatch):
    monkeypatch.setattr(ab, "resolve_budget", lambda *a, **k: ab.Budget(max_remote_spend=100))
    calls = _wire_sequence(monkeypatch, [Response({"cost": 0.001}) for _ in range(20)])
    async def forbidden(*a, **k):
        pytest.fail("reported cost must stop main inference")
        yield
    monkeypatch.setattr(lc, "stream_llm", forbidden)
    events = _run()
    assert calls and root.tokens == 0 and root.remote_spend >= 100
    stop = next(e for e in events if e.get("type") == "budget_exhausted")
    assert stop.get("data", stop)["kind"] == "remote_spend"
    metrics = next(e["data"] for e in events if e.get("type") == "metrics")
    receipts = metrics["compaction_usage"]
    assert all("charged_tokens" not in receipt for receipt in receipts)
    assert sum(r["charged_remote_spend_units"] for r in receipts) == root.remote_spend
