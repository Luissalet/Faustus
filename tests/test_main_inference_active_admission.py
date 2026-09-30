"""Main inference time counts provider awaits, never consumer pauses."""
import asyncio
import json

import pytest

from src import agent_loop as al, autonomy_budget as ab
from tests.test_main_retry_pending_admission import configure
from tests.test_autonomy_budget import _events
from tests.test_degenerate_output import _degenerate_error_chunk


@pytest.mark.parametrize('credit, expected', [(0, 120), (30, 120), (120, 120), (200, 200)])
def test_active_residual_credits_only_actual_same_round_debit(credit, expected):
    pending = {}
    al._pending_main_active_observe(pending, 1, 120)
    ledger = ab.Ledger(active_seconds=credit)
    al._pending_main_usage_settle(pending, 1, tokens=0, remote_units=0, active_seconds=credit)
    view = al._pending_main_admission_view(ledger, pending)
    assert view.active_seconds == expected and ledger.active_seconds == credit
    al._flush_pending_main_usage(ledger, pending)
    al._flush_pending_main_usage(ledger, pending)
    assert ledger.active_seconds == expected and pending == {}


def test_active_credit_does_not_settle_another_outer_round():
    pending = {}
    al._pending_main_active_observe(pending, 1, 40)
    al._pending_main_active_observe(pending, 2, 60)
    ledger = ab.Ledger(active_seconds=100)
    al._pending_main_usage_settle(pending, 1, tokens=0, remote_units=0, active_seconds=100)
    al._flush_pending_main_usage(ledger, pending)
    assert ledger.active_seconds == 160 and pending == {}


@pytest.mark.parametrize('ending', ['done', 'error', 'cancel', 'close'])
def test_measured_iterator_excludes_consumer_pauses_and_close(monkeypatch, ending):
    clock, durations, closed = [1000.0], [], []
    monkeypatch.setattr(al.time, 'monotonic', lambda: clock[0])
    class Provider:
        def __init__(self):
            self.n = 0
        def __aiter__(self):
            return self
        async def __anext__(self):
            self.n += 1
            clock[0] += 20
            if self.n == 1:
                return 'first'
            if ending == 'error':
                raise RuntimeError('provider original')
            if ending == 'cancel':
                raise asyncio.CancelledError()
            raise StopAsyncIteration
        async def aclose(self):
            closed.append(True)
            clock[0] += 400
    async def run():
        stream = al._observe_main_inference_awaits(Provider(), durations.append)
        assert await stream.__anext__() == 'first'
        clock[0] += 1000
        if ending == 'close':
            await stream.aclose()
        else:
            expected = {'done': StopAsyncIteration, 'error': RuntimeError,
                        'cancel': asyncio.CancelledError}[ending]
            with pytest.raises(expected):
                await stream.__anext__()
    asyncio.run(run())
    assert durations == ([20] if ending == 'close' else [20, 20])
    assert closed == [True]


def test_provider_error_survives_close_error_and_observer_error(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(al.time, 'monotonic', lambda: clock[0])
    class Provider:
        def __aiter__(self):
            return self
        async def __anext__(self):
            clock[0] += 5
            raise RuntimeError('provider original')
        async def aclose(self):
            raise ValueError('secondary close')
    def fail_observer(seconds):
        raise LookupError('secondary observer')
    async def run():
        with pytest.raises(RuntimeError, match='provider original'):
            await al._observe_main_inference_awaits(Provider(), fail_observer).__anext__()
    asyncio.run(run())


@pytest.mark.parametrize('trigger', ['degenerate', 'ctx_ack'])
@pytest.mark.parametrize('seconds, paused, expected_calls', [(120, False, 1), (20, False, 2), (20, True, 2)])
def test_real_retry_admission_uses_provider_awaits_only(monkeypatch, trigger, seconds, paused, expected_calls):
    ledger = configure(monkeypatch, ceiling=100000)
    monkeypatch.setattr(ab, 'resolve_budget', lambda *a, **k: ab.Budget(max_active_seconds=100))
    old_setting = al.get_setting
    # Isolate the independent, deliberate wall-clock ceiling from active time.
    monkeypatch.setattr(al, 'get_setting', lambda name, *a, **k:
                        1000000 if name == 'agent_turn_max_seconds' else old_setting(name, *a, **k))
    clock, calls, chunks, effects = [10000.0], [], [], []
    monkeypatch.setattr(al.time, 'monotonic', lambda: clock[0])
    async def execute(*a, **k):
        effects.append(True)
        pytest.fail('The fixture never requests a tool')
    monkeypatch.setattr(al, 'execute_tool_block', execute)
    async def provider(*a, **k):
        calls.append(True)
        clock[0] += seconds
        if len(calls) == 1:
            yield (_degenerate_error_chunk() if trigger == 'degenerate'
                   else 'data: {"delta":"<<faustus_ctx_ack>>"}\n\n')
        else:
            yield 'data: {"delta":"Synthetic terminal answer."}\n\n'
        yield 'data: [DONE]\n\n'
    monkeypatch.setattr(al, 'stream_llm_with_fallback', provider)
    async def run():
        async for chunk in al.stream_agent_loop('https://fixture.invalid/v1', 'm',
                [{'role': 'user', 'content': 'Answer fixture'}], max_rounds=4,
                relevant_tools={'read_file'}, owner='admin'):
            chunks.append(chunk)
            if paused:
                clock[0] += 1000
    asyncio.run(run())
    assert len(calls) == expected_calls and effects == []
    assert ledger.active_seconds == seconds * expected_calls
    stops = [e for e in _events(chunks) if e.get('type') == 'budget_exhausted']
    assert bool(stops) == (expected_calls == 1)
    if stops:
        assert stops[0]['kind'] == 'active_seconds' and stops[0]['used'] == 120


@pytest.mark.parametrize('ending', ['terminal', 'error', 'cancel', 'close'])
def test_real_finalization_records_await_without_usage_event(monkeypatch, ending):
    ledger = configure(monkeypatch, ceiling=100000)
    clock = [10000.0]
    monkeypatch.setattr(al.time, 'monotonic', lambda: clock[0])
    async def provider(*a, **k):
        clock[0] += 37
        if ending == 'error':
            raise RuntimeError('provider original')
        if ending == 'cancel':
            raise asyncio.CancelledError()
        yield 'data: {"delta":"Synthetic terminal answer."}\n\n'
        yield 'data: [DONE]\n\n'
    monkeypatch.setattr(al, 'stream_llm_with_fallback', provider)
    async def run():
        stream = al.stream_agent_loop('https://fixture.invalid/v1', 'm',
            [{'role': 'user', 'content': 'Answer fixture'}], max_rounds=1,
            relevant_tools={'read_file'}, owner='admin')
        async for chunk in stream:
            if ending == 'close' and 'Synthetic terminal answer.' in chunk:
                await stream.aclose()
                break
    if ending in {'error', 'cancel'}:
        with pytest.raises(RuntimeError if ending == 'error' else asyncio.CancelledError):
            asyncio.run(run())
    else:
        asyncio.run(run())
    assert ledger.active_seconds == 37 and ledger.tokens == 0


def test_real_legacy_round_credit_covers_its_inference_without_double_charge(monkeypatch):
    ledger = configure(monkeypatch, ceiling=100000)
    monkeypatch.setattr(ab, 'resolve_budget', lambda *a, **k: ab.Budget(max_active_seconds=1000))
    clock, calls, effects = [10000.0], [], []
    monkeypatch.setattr(al.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(al.time, 'time', lambda: clock[0])
    async def provider(*a, **k):
        calls.append(True)
        clock[0] += 20
        body = ('```read_file\n{"path":"fixture.txt"}\n```' if len(calls) == 1
                else 'Synthetic terminal answer.')
        yield 'data: ' + json.dumps({'delta': body}) + '\n\n'
        yield 'data: [DONE]\n\n'
    async def execute(block, *a, **k):
        effects.append(block.tool_type)
        clock[0] += 180
        return block.tool_type, {'output': 'Synthetic file content', 'exit_code': 0}
    monkeypatch.setattr(al, 'stream_llm_with_fallback', provider)
    monkeypatch.setattr(al, 'execute_tool_block', execute)
    async def run():
        return [chunk async for chunk in al.stream_agent_loop('https://fixture.invalid/v1', 'm',
            [{'role': 'user', 'content': 'Read the fixture'}], max_rounds=2,
            relevant_tools={'read_file'}, owner='admin')]
    events = _events(asyncio.run(run()))
    assert calls == [True, True] and effects == ['read_file']
    # First round: existing 200s covers its 20s inference. Terminal: only 20s.
    assert ledger.active_seconds == 220
    assert not any(e.get('type') == 'budget_exhausted' for e in events)


def test_no_observer_keeps_legacy_items_without_sampling_clock(monkeypatch):
    from types import SimpleNamespace
    def no_clock():
        pytest.fail('No observer requests no timing')
    monkeypatch.setattr(al, 'time', SimpleNamespace(monotonic=no_clock))
    item = {'legacy': True}
    async def provider():
        yield item
    async def run():
        return [chunk async for chunk in al._observe_main_inference_awaits(provider())]
    result = asyncio.run(run())
    assert result == [item] and result[0] is item
