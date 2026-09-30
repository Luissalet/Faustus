"""Observed main residuals settle once into the exact in-memory turn ledger."""
import asyncio

import pytest

from src import agent_loop as al, autonomy_budget as ab, llm_core as lc
from tests.test_autonomy_budget import _patch_common, _collect, _events
from tests.test_degenerate_output import _degenerate_error_chunk
from tests.test_recovery_budget_receipts import usage


@pytest.mark.parametrize('tokens_credit,spend_credit', [(0, 0), (230, 1000), (1000, 230), (1000, 1000)])
def test_flush_only_residual_dimensions_and_repeated_calls_are_noops(tokens_credit, spend_credit):
    pending = {}
    al._pending_main_usage_update(pending, 1, {'total_tokens': 1000}, endpoint_cost_tracked=True)
    ledger = ab.Ledger(tokens=tokens_credit, remote_spend=spend_credit)
    al._pending_main_usage_settle(pending, 1, tokens=tokens_credit, remote_units=spend_credit)
    al._flush_pending_main_usage(ledger, pending)
    al._flush_pending_main_usage(ledger, pending)
    assert ledger.tokens == ledger.remote_spend == 1000 and pending == {}


def test_partial_dimension_failure_does_not_charge_tokens_again(monkeypatch):
    pending = {}
    al._pending_main_usage_update(pending, 1, {'total_tokens': 1000}, endpoint_cost_tracked=True)
    ledger = ab.Ledger()
    real_add = ledger.add_remote_spend
    def fail_before_add(units):
        raise ValueError('synthetic dimension failure')
    monkeypatch.setattr(ledger, 'add_remote_spend', fail_before_add)
    with pytest.raises(ValueError):
        al._flush_pending_main_usage(ledger, pending)
    assert ledger.tokens == 1000 and ledger.remote_spend == 0
    monkeypatch.setattr(ledger, 'add_remote_spend', real_add)
    al._flush_pending_main_usage(ledger, pending)
    assert ledger.tokens == ledger.remote_spend == 1000 and pending == {}


@pytest.mark.parametrize('tracked, expected', [(True, 500), (False, 0), (None, 0)])
def test_cost_only_uses_existing_remote_policy_without_inventing_tokens(tracked, expected):
    pending = {}
    al._pending_main_usage_update(pending, 1, {'cost_usd': 0.001}, endpoint_cost_tracked=tracked)
    ledger = ab.Ledger()
    al._flush_pending_main_usage(ledger, pending)
    al._flush_pending_main_usage(ledger, pending)
    assert ledger.tokens == 0 and ledger.remote_spend == pytest.approx(expected)
    assert pending == {}


@pytest.mark.parametrize('ending', ['terminal', 'degenerate', 'ctx_ack', 'close', 'cancel', 'error'])
def test_real_wrapper_settles_observed_usage_on_every_ending(monkeypatch, ending):
    _patch_common(monkeypatch)
    ledger = ab.Ledger()
    monkeypatch.setattr(ab, 'Ledger', lambda: ledger)
    monkeypatch.setattr(ab, 'resolve_budget', lambda *a, **k: ab.Budget(max_tokens=500))
    async def auxiliary(*a, **k):
        return 'Synthetic auxiliary answer.'
    monkeypatch.setattr(lc, 'llm_call_async', auxiliary)
    calls = []
    async def stream(*a, **k):
        calls.append(True)
        assert len(calls) == 1
        yield usage({'input_tokens': 800, 'output_tokens': 200})
        if ending == 'degenerate':
            yield _degenerate_error_chunk()
        elif ending == 'ctx_ack':
            yield 'data: {"delta":"<<faustus_ctx_ack>>"}\n\n'
        elif ending == 'cancel':
            raise asyncio.CancelledError()
        elif ending == 'error':
            raise RuntimeError('synthetic original stream failure')
        else:
            yield 'data: {"delta":"Synthetic terminal answer."}\n\n'
        yield 'data: [DONE]\n\n'
    monkeypatch.setattr(al, 'stream_llm_with_fallback', stream)
    gen = al.stream_agent_loop('https://fixture.invalid/v1', 'm',
        [{'role': 'user', 'content': 'Answer the fixture'}], max_rounds=3,
        relevant_tools={'read_file'}, owner='admin',
        route_descriptors=[{'endpoint_cost_tracked': True}])
    if ending == 'close':
        async def close():
            async for chunk in gen:
                if 'Synthetic terminal answer.' in chunk:
                    await gen.aclose()
                    return
        asyncio.run(close())
    elif ending in {'cancel', 'error'}:
        expected = asyncio.CancelledError if ending == 'cancel' else RuntimeError
        with pytest.raises(expected):
            _collect(gen)
    else:
        events = _events(_collect(gen))
        assert any(e.get('type') == 'budget_exhausted' for e in events) == (ending != 'terminal')
    assert ledger.tokens == ledger.remote_spend == 1000
    assert calls == [True] and al._TURN_FINALIZERS.get() is None


def test_normal_flush_precedes_metrics_and_wrapper_repeat_does_not_double_charge(monkeypatch):
    _patch_common(monkeypatch)
    ledger = ab.Ledger()
    monkeypatch.setattr(ab, 'Ledger', lambda: ledger)
    async def stream(*a, **k):
        yield usage({'input_tokens': 30, 'output_tokens': 20})
        yield 'data: {"delta":"Synthetic terminal answer."}\n\n'
        yield 'data: [DONE]\n\n'
    monkeypatch.setattr(al, 'stream_llm_with_fallback', stream)
    async def run():
        async for chunk in al.stream_agent_loop('https://fixture.invalid/v1', 'm',
                [{'role': 'user', 'content': 'Answer fixture'}], max_rounds=1,
                relevant_tools={'read_file'}, owner='admin'):
            if '"type": "metrics"' in chunk:
                assert ledger.tokens == 50
    asyncio.run(run())
    assert ledger.tokens == 50


def test_local_terminal_records_tokens_without_changing_unbounded_grant(monkeypatch):
    _patch_common(monkeypatch)
    ledger = ab.Ledger()
    monkeypatch.setattr(ab, 'Ledger', lambda: ledger)
    async def stream(*a, **k):
        yield usage({'input_tokens': 1000, 'output_tokens': 0})
        yield 'data: {"delta":"Synthetic terminal answer."}\n\n'
        yield 'data: [DONE]\n\n'
    monkeypatch.setattr(al, 'stream_llm_with_fallback', stream)
    events = _events(_collect(al.stream_agent_loop('http://127.0.0.1:8888/v1', 'm',
        [{'role': 'user', 'content': 'Answer fixture'}], max_rounds=1,
        relevant_tools={'read_file'}, owner='admin', route_descriptors=[{'endpoint_cost_tracked': False}])))
    assert ledger.tokens == 1000 and ledger.remote_spend == 0
    assert not any(e.get('type') == 'budget_exhausted' for e in events)


@pytest.mark.parametrize('body_error,close_error', [(False, True), (True, False), (True, True)])
def test_close_failure_still_runs_all_finalizers_in_order_without_masking_original(monkeypatch, body_error, close_error):
    _patch_common(monkeypatch)
    order = []
    class Stream:
        def __aiter__(self):
            return self
        async def __anext__(self):
            if body_error:
                raise RuntimeError('original body error')
            raise StopAsyncIteration
        async def aclose(self):
            order.append('close')
            if close_error:
                raise ValueError('original close error')
    def body(*a, **k):
        def broken():
            order.append('broken')
            raise LookupError('finalizer error must not mask original')
        al._TURN_FINALIZERS.get().extend([lambda: order.append('first'), broken,
                                         lambda: order.append('last')])
        return Stream()
    monkeypatch.setattr(al, '_stream_agent_loop_body', body)
    expected = RuntimeError if body_error else ValueError
    with pytest.raises(expected, match='original body error' if body_error else 'original close error'):
        _collect(al.stream_agent_loop())
    assert order == ['close', 'first', 'broken', 'last']
    assert al._TURN_FINALIZERS.get() is None
