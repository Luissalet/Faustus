"""Recovery-ladder provider awaits join the round's measured active time."""
import asyncio

import pytest

from src import agent_loop as al, autonomy_budget as ab, llm_core as lc
from tests.test_main_retry_pending_admission import configure
from tests.test_autonomy_budget import _events
from tests.test_degenerate_output import _degenerate_error_chunk


def _loop(monkeypatch, *, main_seconds, step2_seconds, trigger='degenerate', step3_seconds=10):
    ledger = configure(monkeypatch, ceiling=100000)
    monkeypatch.setattr(ab, 'resolve_budget', lambda *a, **k: ab.Budget(max_active_seconds=100))
    old_setting = al.get_setting
    monkeypatch.setattr(al, 'get_setting', lambda name, *a, **k:
                        1000000 if name == 'agent_turn_max_seconds' else old_setting(name, *a, **k))
    monkeypatch.setattr('src.endpoint_resolver.resolve_endpoint',
                        lambda *a, **k: ('https://utility.invalid/v1', 'utility', {}))
    async def auxiliary(*a, **k):
        return 'Synthetic auxiliary answer.'
    monkeypatch.setattr(lc, 'llm_call_async', auxiliary)
    clock, calls, effects = [10000.0], [], []
    monkeypatch.setattr(al.time, 'monotonic', lambda: clock[0])
    async def execute(*a, **k):
        effects.append(True)
        pytest.fail('The fixture never requests a tool')
    monkeypatch.setattr(al, 'execute_tool_block', execute)
    main_count = 3 if trigger == 'ctx_ack' else 2
    async def provider(candidates, *a, **k):
        calls.append(candidates[0][0])
        n = len(calls)
        if n <= main_count:
            clock[0] += main_seconds
            if trigger == 'ctx_ack':
                yield 'data: {"delta":"<<faustus_ctx_ack>>"}\n\n'
                yield 'data: [DONE]\n\n'
            else:
                yield _degenerate_error_chunk().replace('started repeating tokens', 'reasoning loop')
        elif n == main_count + 1:
            clock[0] += step2_seconds  # Step 2 (same endpoint, tools off) fails too.
            yield 'data: {"delta":"<<faustus_ctx_ack>>"}\n\n'
            yield 'data: [DONE]\n\n'
        else:
            clock[0] += step3_seconds
            yield 'data: {"delta":"Recovered synthetic answer."}\n\n'
            yield 'data: [DONE]\n\n'
    monkeypatch.setattr(al, 'stream_llm_with_fallback', provider)
    chunks = []
    async def run():
        async for chunk in al.stream_agent_loop('https://fixture.invalid/v1', 'm',
                [{'role': 'user', 'content': 'Answer fixture'}], max_rounds=5,
                relevant_tools={'read_file'}, owner='admin'):
            chunks.append(chunk)
            clock[0] += 500  # Consumer pauses never count.
    asyncio.run(run())
    return ledger, calls, effects, _events(chunks), main_count


@pytest.mark.parametrize('trigger', ['degenerate', 'ctx_ack'])
def test_step2_await_counts_before_step3_admission(monkeypatch, trigger):
    main = 20 if trigger == 'degenerate' else 10
    ledger, calls, effects, events, main_count = _loop(
        monkeypatch, main_seconds=main, step2_seconds=70, trigger=trigger)
    # main_count*main + 70 >= 100: the utility step is never started.
    assert len(calls) == main_count + 1 and effects == []
    assert 'https://utility.invalid/v1' not in calls
    stops = [e for e in events if e.get('type') == 'budget_exhausted']
    assert stops and stops[0]['kind'] == 'active_seconds'
    assert stops[0]['used'] == pytest.approx(main_count * main + 70, abs=1)
    assert ledger.active_seconds == pytest.approx(main_count * main + 70, abs=1)


@pytest.mark.parametrize('trigger', ['degenerate', 'ctx_ack'])
def test_short_recovery_admitted_and_charged_once(monkeypatch, trigger):
    main = 20 if trigger == 'degenerate' else 10
    ledger, calls, effects, events, main_count = _loop(
        monkeypatch, main_seconds=main, step2_seconds=30, trigger=trigger)
    assert len(calls) == main_count + 2 and calls[-1] == 'https://utility.invalid/v1'
    assert not any(e.get('type') == 'budget_exhausted' for e in events)
    assert any(e.get('type') == 'response_replace' and 'Recovered synthetic answer.' in e.get('text', '')
               for e in events)
    # Main + step 2 + step 3 provider awaits, once; consumer pauses excluded.
    assert ledger.active_seconds == pytest.approx(main_count * main + 30 + 10, abs=1)


def test_recovery_step_without_observer_keeps_unwrapped_stream(monkeypatch):
    seen = []
    async def provider(*a, **k):
        yield 'data: {"delta":"Plain recovery."}\n\n'
    monkeypatch.setattr(al, 'stream_llm_with_fallback', provider)
    monkeypatch.setattr(al, '_observe_main_inference_awaits',
                        lambda *a, **k: pytest.fail('no observer must not wrap'))
    text, *_ = asyncio.run(al._recovery_step_completion(
        'https://fixture.invalid/v1', 'm', {}, [], 0.7, 16, {}, None, 30))
    assert text == 'Plain recovery.'


def test_recovery_step_observer_measures_provider_awaits(monkeypatch):
    clock, seen = [0.0], []
    monkeypatch.setattr(al.time, 'monotonic', lambda: clock[0])
    async def provider(*a, **k):
        clock[0] += 12
        yield 'data: {"delta":"Measured "}\n\n'
        clock[0] += 3
        yield 'data: {"delta":"recovery."}\n\n'
    monkeypatch.setattr(al, 'stream_llm_with_fallback', provider)
    text, *_ = asyncio.run(al._recovery_step_completion(
        'https://fixture.invalid/v1', 'm', {}, [], 0.7, 16, {}, None, 30,
        _active_observer=seen.append))
    assert text == 'Measured recovery.' and sum(seen) == 15
