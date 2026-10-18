"""Activity means provider output, not comments or an early assistant role frame."""
import asyncio
import json

import pytest

from src import llm_core
from tests.test_llm_core_stream_stall import _drive, _sse


@pytest.mark.parametrize('opening', ['', ': keepalive', _sse({'role':'assistant'}), _sse({})])
def test_empty_prefill_frames_do_not_start_mid_generation_idle_cap(monkeypatch, opening):
    events = _drive(monkeypatch, [(0,opening),(.4,_sse({'content':'answer'})),
                                  (0,'data: [DONE]')],idle=.2)
    assert not any(e.get('error_class') for e in events),events
    assert any(e.get('delta') == 'answer' for e in events)


@pytest.mark.parametrize('keepalive', [': still here', '', _sse({}), 'data: {"choices":[],"usage":{}}'])
def test_empty_frames_after_real_output_cannot_extend_idle(monkeypatch, keepalive):
    script = [(0,_sse({'reasoning_content':'actual thought'}))]
    script += [(.02,keepalive)] * 20
    script += [(0,_sse({'content':'too late'})),(0,'data: [DONE]')]
    events = _drive(monkeypatch,script,idle=.2)
    failures = [e for e in events if e.get('error_class') == 'timeout.inactivity']
    assert len(failures) == 1,events
    assert failures[0]['partial'] is True and failures[0]['retryable'] is False
    assert not any(e.get('error_class') == llm_core.DEGENERATE_OUTPUT_ERROR_CLASS for e in events)
    assert not any(e.get('delta') == 'too late' for e in events)


@pytest.mark.parametrize('delta', [
    {'content':'x'}, {'reasoning_content':'x'}, {'reasoning':'x'}, {'thinking':'x'},
    {'tool_calls':[{'index':0,'function':{'arguments':' '}}]},
])
def test_continuous_real_output_keeps_idle_alive(monkeypatch, delta):
    script = [(0,_sse(delta))] + [(.02,_sse(delta))] * 20
    script += [(0,'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}'),(0,'data: [DONE]')]
    events = _drive(monkeypatch,script,idle=.2)
    assert not any(e.get('error_class') for e in events),events


def test_inactivity_has_transport_evidence_without_claiming_corruption():
    from src.stream_recovery import interrupted_response
    chunk = llm_core._stream_stalled_error_chunk('fixture','http://fixture.invalid',120,partial=True)
    event = json.loads(chunk.split('data: ',1)[1])
    assert event['error_class'] == 'timeout.inactivity'
    assert event['timeout_kind'] == 'inactivity' and event['status'] == 504
    assert interrupted_response(event)
    assert 'while the server kept working' not in event['error']


def test_timer_closes_the_pending_upstream_iterator():
    async def run():
        closed=[]
        async def lines():
            try:
                yield _sse({'content':'started'})
                await asyncio.sleep(10)
            finally:
                closed.append(True)
        result = [line async for line in llm_core._lines_until_stall(lines(),.03)]
        assert result[-1] is llm_core._STREAM_STALLED and closed == [True]
    asyncio.run(run())
