"""A main retry must not ignore its already observed, unsettled usage."""
import asyncio
import json

import pytest

from src import agent_loop as al, autonomy_budget as ab, llm_core as lc
from tests.test_autonomy_budget import _patch_common, _collect, _events
from tests.test_degenerate_output import _degenerate_error_chunk
from tests.test_recovery_budget_receipts import usage


def configure(monkeypatch, *, ceiling=500):
    _patch_common(monkeypatch)
    ledger = ab.Ledger()
    monkeypatch.setattr(ab, 'Ledger', lambda: ledger)
    monkeypatch.setattr(ab, 'resolve_budget', lambda *a, **k: ab.Budget(max_tokens=ceiling))
    async def auxiliary(*a, **k):
        return 'Synthetic auxiliary answer.'
    monkeypatch.setattr(lc, 'llm_call_async', auxiliary)
    return ledger


def run(*, local=False):
    return _events(_collect(al.stream_agent_loop(
        'http://127.0.0.1:8888/v1' if local else 'https://fixture.invalid/v1', 'm',
        [{'role': 'user', 'content': 'Answer the fixture'}], max_rounds=4,
        relevant_tools={'read_file'}, owner='admin')))


@pytest.mark.parametrize('trigger', ['degenerate', 'reasoning_loop', 'ctx_ack', 'ctx_ack_clean'])
@pytest.mark.parametrize('amount, denied', [(1000, True), (100, False)])
def test_main_retry_is_admitted_only_below_pending_observed_limit(monkeypatch, trigger, amount, denied):
    ledger = configure(monkeypatch)
    calls, effects = [], []
    bad_rounds = 2 if trigger == 'ctx_ack_clean' else 1
    async def execute(*a, **k):
        effects.append(True)
        pytest.fail('No tool effect is part of this fixture')
    monkeypatch.setattr(al, 'execute_tool_block', execute)
    async def stream(*a, **k):
        calls.append(ledger.tokens)
        if denied:
            assert len(calls) == 1, 'Already observed usage must deny the first retry'
        if len(calls) <= bad_rounds:
            yield usage({'input_tokens': amount, 'output_tokens': 0})
            if trigger.startswith('ctx_ack'):
                yield 'data: {"delta":"<<faustus_ctx_ack>>"}\n\n'
            else:
                error = _degenerate_error_chunk()
                if trigger == 'reasoning_loop':
                    error = error.replace('started repeating tokens', 'reasoning loop')
                yield error
        else:
            yield 'data: {"delta":"Synthetic main retry answer."}\n\n'
        yield 'data: [DONE]\n\n'
    monkeypatch.setattr(al, 'stream_llm_with_fallback', stream)
    events = run()
    assert len(calls) == (1 if denied else bad_rounds + 1) and effects == []
    assert any(e.get('type') == 'budget_exhausted' for e in events) == denied
    assert not any(e.get('status') == 'recovery' for e in events)
    if denied:
        stop, = [e for e in events if e.get('type') == 'budget_exhausted']
        assert stop['kind'] == 'tokens' and stop['used'] == 1000
        assert ledger.tokens == 1000  # Finalization now settles the observed residual.


def test_clean_context_retry_checks_accumulated_outer_rounds(monkeypatch):
    ledger = configure(monkeypatch)
    calls = []
    async def stream(*a, **k):
        calls.append(ledger.tokens)
        assert len(calls) <= 2, 'The third main call would exceed observed usage'
        yield usage({'input_tokens': 300, 'output_tokens': 0})
        yield 'data: {"delta":"<<faustus_ctx_ack>>"}\n\n'
        yield 'data: [DONE]\n\n'
    monkeypatch.setattr(al, 'stream_llm_with_fallback', stream)
    events = run()
    assert calls == [0, 0]
    stop, = [e for e in events if e.get('type') == 'budget_exhausted']
    assert stop['used'] == 600 and ledger.tokens == 600


@pytest.mark.parametrize('snapshots, denied, bound', [
    ([{'input_tokens': 800}, {'output_tokens': 200}], True, 1000),
    ([{'input_tokens': 800, 'output_tokens': 200}] * 2, True, 1000),
    ([{'total_tokens': 1000}, {'input_tokens': 800, 'output_tokens': 400}], True, 1200),
    ([{'input_tokens': True, 'output_tokens': -1}], False, None),
])
def test_main_gate_uses_valid_snapshots_not_estimates_or_duplicate_sums(monkeypatch, snapshots, denied, bound):
    ledger = configure(monkeypatch)
    calls = []
    async def stream(*a, **k):
        calls.append(True)
        assert not denied or len(calls) == 1
        if len(calls) == 1:
            for snapshot in snapshots:
                yield usage(snapshot)
            yield _degenerate_error_chunk()
        else:
            yield 'data: {"delta":"Synthetic main retry answer."}\n\n'
        yield 'data: [DONE]\n\n'
    monkeypatch.setattr(al, 'stream_llm_with_fallback', stream)
    events = run()
    stops = [e for e in events if e.get('type') == 'budget_exhausted']
    assert bool(stops) == denied
    if denied:
        assert stops[0]['used'] == bound and ledger.tokens == bound
    else:
        assert len(calls) == 2


@pytest.mark.parametrize('trigger', ['degenerate', 'ctx_ack'])
def test_local_turn_keeps_existing_unbounded_policy(monkeypatch, trigger):
    configure(monkeypatch)
    calls = []
    async def stream(*a, **k):
        calls.append(True)
        if len(calls) == 1:
            yield usage({'input_tokens': 1000, 'output_tokens': 0})
            if trigger == 'degenerate':
                yield _degenerate_error_chunk()
            else:
                yield 'data: {"delta":"<<faustus_ctx_ack>>"}\n\n'
        else:
            yield 'data: {"delta":"Synthetic main retry answer."}\n\n'
        yield 'data: [DONE]\n\n'
    monkeypatch.setattr(al, 'stream_llm_with_fallback', stream)
    events = run(local=True)
    assert len(calls) == 2
    assert not any(e.get('type') == 'budget_exhausted' for e in events)


def test_previous_settlement_does_not_deny_the_next_main_round_twice(monkeypatch):
    ledger = configure(monkeypatch, ceiling=45)
    calls = []
    async def stream(*a, **k):
        calls.append(ledger.tokens)
        if len(calls) == 1:
            yield usage({'input_tokens': 20, 'output_tokens': 10})
            yield 'data: '+json.dumps({'delta': '```read_file\n{"path":"synthetic.txt"}\n```'})+'\n\n'
        else:
            yield 'data: {"delta":"Synthetic next-round answer."}\n\n'
        yield 'data: [DONE]\n\n'
    monkeypatch.setattr(al, 'stream_llm_with_fallback', stream)
    events = run()
    assert calls == [0, 30]
    assert not any(e.get('type') == 'budget_exhausted' for e in events)


@pytest.mark.parametrize('amount', [100, 1000])
def test_observed_cancel_still_prevents_the_retry(monkeypatch, amount):
    configure(monkeypatch)
    cancelled, calls = [False], []
    async def stream(*a, **k):
        calls.append(True)
        assert len(calls) == 1, 'A stopped turn must not infer its retry'
        yield usage({'input_tokens': amount, 'output_tokens': 0})
        yield _degenerate_error_chunk()
    monkeypatch.setattr(al, 'stream_llm_with_fallback', stream)
    async def consume():
        chunks = []
        async for chunk in al.stream_agent_loop('https://fixture.invalid/v1', 'm',
            [{'role': 'user', 'content': 'Answer the fixture'}], max_rounds=3,
            relevant_tools={'read_file'}, owner='admin',
            pending_cancel=lambda: 'task_cancelled' if cancelled[0] else None):
            chunks.append(chunk)
            if '"reason": "degenerate_output_retry"' in chunk:
                cancelled[0] = True
        return _events(chunks)
    events = asyncio.run(consume())
    assert calls == [True]
    assert any(e.get('type') == 'cancelled' for e in events)
