"""Observed recovery snapshots charge the captured turn, without real models."""
import asyncio
import json

import pytest

from src import agent_loop as al, autonomy_budget as ab, llm_core as lc
from tests.test_autonomy_budget import _patch_common, _collect, _events
from tests.test_degenerate_output import _degenerate_error_chunk


def usage(value):
    return 'data: ' + json.dumps({'type': 'usage', 'data': value}) + '\n\n'


def completion(observer=None):
    return al._recovery_step_completion('https://fixture.invalid/v1', 'm', {}, [],
                                       0.7, 100, {}, 'fixture', 10,
                                       _usage_observer=observer)


@pytest.mark.parametrize('tail', ['done', 'error', 'cancel'])
def test_last_snapshot_once_even_after_error_or_cancellation(monkeypatch, tail):
    async def stream(*a, **k):
        yield usage({'input_tokens': 20, 'output_tokens': 5})
        yield usage({'input_tokens': 20, 'output_tokens': 10})
        yield usage({'input_tokens': 20, 'output_tokens': 10})
        if tail == 'cancel':
            raise asyncio.CancelledError()
        if tail == 'error':
            yield 'event: error\ndata: {"error":"synthetic"}\n\n'
        else:
            yield 'data: {"delta":"answer"}\n\n'
    monkeypatch.setattr(al, 'stream_llm_with_fallback', stream)
    seen = []
    if tail == 'cancel':
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(completion(lambda u, **k: seen.append((u, k))))
    else:
        result = asyncio.run(completion(lambda u, **k: seen.append((u, k))))
        assert len(result) == 4 and result[-1] == (tail == 'error')
    assert len(seen) == 1
    assert seen[0][0]['input_tokens'] == 20 and seen[0][0]['output_tokens'] == 10
    assert seen[0][0]['cost_state'] == 'unknown' and 'cost_usd' not in seen[0][0]
    assert seen[0][1] == {'endpoint_local': False}


@pytest.mark.parametrize('raw,expected', [
    ({}, {}), ({'input_tokens': True}, {}), ({'input_tokens': -1}, {}),
    ({'input_tokens': float('nan')}, {}), ({'total_tokens': 2**63}, {}),
    ({'input_tokens': '20'}, {}), ({'output_tokens': 1.5}, {}),
    ({'input_tokens': 30, 'output_tokens': True}, {'input_tokens': 30}),
    ({'total_tokens': 90}, {'total_tokens': 90}),
    ({'cost_usd': 0.001}, {'cost_usd': 0.001}),
    ({'cost_usd': True}, {}), ({'cost_usd': 10**400}, {}),
])
def test_snapshot_keeps_only_observed_fields(raw, expected):
    result = al._recovery_usage_snapshot(raw)
    assert {k: v for k, v in result.items() if k.endswith('_tokens') or k == 'cost_usd'} == expected
    assert bool(result) == bool(expected)


def test_legacy_return_and_failed_observer_are_compatible(monkeypatch):
    async def stream(*a, **k):
        yield usage({'input_tokens': 10})
        yield 'data: {"delta":"answer"}\n\n'
    monkeypatch.setattr(al, 'stream_llm_with_fallback', stream)
    def broken(*a, **k):
        raise ValueError('synthetic observer')
    assert asyncio.run(completion()) == ('answer', '', False, False)
    assert asyncio.run(completion(broken)) == ('answer', '', False, False)


@pytest.mark.parametrize('snapshots,expected', [
    ([{'input_tokens': 800, 'output_tokens': 200}, {'cost_usd': 0.001}],
     {'input_tokens': 800, 'output_tokens': 200, 'cost_usd': 0.001}),
    ([{'input_tokens': 800}, {'output_tokens': 200}],
     {'input_tokens': 800, 'output_tokens': 200}),
    ([{'input_tokens': 800, 'output_tokens': 200}, {'input_tokens': True, 'output_tokens': -1}],
     {'input_tokens': 800, 'output_tokens': 200}),
    ([{'cost_usd': 0.001}, {'input_tokens': 800}],
     {'input_tokens': 800, 'cost_usd': 0.001}),
])
def test_partial_snapshots_preserve_previously_observed_dimensions(monkeypatch, snapshots, expected):
    async def stream(*a, **k):
        for snapshot in snapshots:
            yield usage(snapshot)
    monkeypatch.setattr(al, 'stream_llm_with_fallback', stream)
    seen = []
    asyncio.run(completion(lambda u, **k: seen.append(u)))
    observed, = seen
    assert {k: v for k, v in observed.items() if k.endswith('_tokens') or k == 'cost_usd'} == expected
    assert observed['cost_state'] == ('known' if 'cost_usd' in expected else 'unknown')


@pytest.mark.parametrize('exhaust_after_first', [False, True])
def test_ladder_separate_attempts_and_gate_before_utility(monkeypatch, exhaust_after_first):
    monkeypatch.setattr(al, '_assemble_prompt', lambda *a, **k: 'synthetic')
    monkeypatch.setattr('src.endpoint_resolver.resolve_endpoint',
                        lambda *a, **k: ('https://utility.invalid/v1', 'utility', {}))
    ledger = ab.Ledger()
    calls, seen = [], []
    async def stream(candidates, *a, **k):
        calls.append(candidates[0][1])
        yield usage({'input_tokens': 40, 'output_tokens': 10})
        if len(calls) == 1:
            yield 'event: error\ndata: {"error":"synthetic"}\n\n'
        else:
            yield 'data: {"delta":"answer"}\n\n'
    monkeypatch.setattr(al, 'stream_llm_with_fallback', stream)
    def charge(u, *, step, endpoint_local):
        seen.append(step)
        ledger.add_tokens(u['input_tokens'] + u['output_tokens'])
    async def run():
        return [item async for item in al._recovery_ladder(reason='ctx_ack',
            endpoint_url='https://fixture.invalid/v1', model='m', headers={},
            messages=[], temperature=0.7, max_tokens=100, gen_overrides={},
            session_id='fixture', owner='alice', agent_stream_timeout=10,
            _usage_observer=charge,
            _admission_check=lambda url: ledger.check(ab.Budget(max_tokens=50 if exhaust_after_first else 200)))]
    result = asyncio.run(run())[-1][1]
    assert calls == (['m'] if exhaust_after_first else ['m', 'utility'])
    assert seen == ([2] if exhaust_after_first else [2, 3])
    assert ledger.tokens == (50 if exhaust_after_first else 100)
    assert bool(result.get('budget_exhaustion')) == exhaust_after_first


@pytest.mark.parametrize('local_recovery,cost_only,local_main', [
    (False, False, False), (True, False, False), (False, True, False), (False, False, True),
])
def test_real_root_ledger_and_metrics_receive_recovery_only(monkeypatch, local_recovery, cost_only, local_main):
    _patch_common(monkeypatch)
    ledger = ab.Ledger()
    monkeypatch.setattr(ab, 'Ledger', lambda: ledger)
    monkeypatch.setattr(ab, 'resolve_budget', lambda *a, **k: ab.Budget(max_tokens=10000))
    utility_url = 'http://127.0.0.1:9999/v1' if local_recovery else 'https://utility.invalid/v1'
    monkeypatch.setattr('src.endpoint_resolver.resolve_endpoint', lambda *a, **k: (utility_url, 'utility', {}))
    async def auxiliary(*a, **k):
        return 'Synthetic auxiliary answer.'
    monkeypatch.setattr(lc, 'llm_call_async', auxiliary)
    calls = []
    async def stream(candidates, *a, **k):
        calls.append(candidates[0][1])
        if len(calls) < 3:
            yield _degenerate_error_chunk()
        else:
            yield usage({'cost_usd': 0.001} if cost_only else {'input_tokens': 800, 'output_tokens': 200})
            yield 'data: {"delta":"Recovered synthetic answer."}\n\n'
            yield 'data: [DONE]\n\n'
    monkeypatch.setattr(al, 'stream_llm_with_fallback', stream)
    main_url = 'http://127.0.0.1:8888/v1' if local_main else 'https://fixture.invalid/v1'
    events = _events(_collect(al.stream_agent_loop(main_url, 'm',
        [{'role': 'user', 'content': 'Answer the fixture'}], max_rounds=2,
        relevant_tools={'read_file'}, owner='admin')))
    assert calls == ['m', 'm', 'utility']
    metrics = next(e['data'] for e in events if e.get('type') == 'metrics')
    assert any(e.get('type') == 'response_replace' for e in events)
    assert ledger.tokens == (0 if local_recovery or cost_only else 1000)
    if local_recovery:
        assert 'recovery_usage' not in metrics
    else:
        receipt, = metrics['recovery_usage']
        assert receipt['step'] == 3 and receipt['phase'] == 'recovery'
        if cost_only:
            assert 'charged_tokens' not in receipt and ledger.remote_spend == pytest.approx(500)
        else:
            assert receipt['charged_tokens'] == 1000 and ledger.remote_spend == 1000
            assert 'cost_usd' not in receipt
    # Auxiliary counts stay separate from existing main-stream estimates.
    assert metrics['total_tokens'] < 1000


def test_cancellation_before_usage_does_not_invent_receipt(monkeypatch):
    async def stream(*a, **k):
        raise asyncio.CancelledError()
        yield
    monkeypatch.setattr(al, 'stream_llm_with_fallback', stream)
    seen = []
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(completion(lambda u, **k: seen.append(u)))
    assert seen == []


def test_first_step_admission_prevents_inference(monkeypatch):
    monkeypatch.setattr(al, '_assemble_prompt', lambda *a, **k: 'synthetic')
    async def forbidden(*a, **k):
        pytest.fail('An exhausted budget must reject the first recovery step')
        yield
    monkeypatch.setattr(al, 'stream_llm_with_fallback', forbidden)
    ledger = ab.Ledger(tokens=100)
    async def run():
        return [item async for item in al._recovery_ladder(reason='ctx_ack',
            endpoint_url='https://fixture.invalid/v1', model='m', headers={},
            messages=[], temperature=0.7, max_tokens=100, gen_overrides={},
            session_id='fixture', owner='alice', agent_stream_timeout=10,
            _admission_check=lambda url: ledger.check(ab.Budget(max_tokens=100)))]
    assert asyncio.run(run())[-1][1]['budget_exhaustion'].kind == 'tokens'


@pytest.mark.parametrize('trigger', ['degenerate', 'ctx_ack'])
def test_real_root_stops_before_utility_after_same_model_recovery_spends_budget(monkeypatch, trigger):
    _patch_common(monkeypatch)
    ledger = ab.Ledger()
    monkeypatch.setattr(ab, 'Ledger', lambda: ledger)
    monkeypatch.setattr(ab, 'resolve_budget', lambda *a, **k: ab.Budget(max_tokens=100))
    monkeypatch.setattr('src.endpoint_resolver.resolve_endpoint',
                        lambda *a, **k: ('https://utility.invalid/v1', 'utility', {}))
    async def auxiliary(*a, **k):
        return 'Synthetic auxiliary answer.'
    monkeypatch.setattr(lc, 'llm_call_async', auxiliary)
    calls, effects = [], []
    main_calls = 3 if trigger == 'ctx_ack' else 2
    async def execute(*a, **k):
        effects.append(True)
        pytest.fail('No tool may run after recovery exhausts the budget')
    monkeypatch.setattr(al, 'execute_tool_block', execute)
    async def stream(candidates, *a, **k):
        calls.append(candidates[0][1])
        assert len(calls) <= main_calls + 1, 'No utility or later round may infer'
        if len(calls) <= main_calls:
            if trigger == 'ctx_ack':
                yield 'data: {"delta":"<<faustus_ctx_ack>>"}\n\n'
                yield 'data: [DONE]\n\n'
            else:
                yield _degenerate_error_chunk().replace('started repeating tokens', 'reasoning loop')
        else:
            yield usage({'input_tokens': 80, 'output_tokens': 20})
            yield 'event: error\ndata: {"error":"synthetic"}\n\n'
    monkeypatch.setattr(al, 'stream_llm_with_fallback', stream)
    events = _events(_collect(al.stream_agent_loop('https://fixture.invalid/v1', 'm',
        [{'role': 'user', 'content': 'Answer the fixture'}], max_rounds=4,
        relevant_tools={'read_file'}, owner='admin')))
    assert calls == ['m'] * (main_calls + 1) and effects == []
    assert ledger.tokens == 100
    stop, = [e for e in events if e.get('type') == 'budget_exhausted']
    assert stop['kind'] == 'tokens'
    assert not any(e.get('type') == 'agent_terminal' for e in events)
    metrics = next(e['data'] for e in events if e.get('type') == 'metrics')
    assert metrics['recovery_usage'][0]['step'] == 2
