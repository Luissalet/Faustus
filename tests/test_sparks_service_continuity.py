"""A typed temporary Sparks outage does not finish a task or change its model."""
import asyncio
import json

import pytest

from src import llm_core as core


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['connect', 'read', '503'])
@pytest.mark.parametrize('reasoning_budget', [4096, 0, None])
async def test_thinking_budget_does_not_replace_transport_retry_budget(monkeypatch, failure, reasoning_budget):
    import httpx
    from tests.test_thinking_inertia import _Client

    payloads = []

    class FailedContext:
        async def __aenter__(self):
            if failure == '503':
                class Response:
                    status_code = 503
                    headers = {}

                    async def aread(self):
                        return b'{"error":{"message":"temporary service unavailable"}}'
                return Response()
            raise (httpx.ConnectTimeout if failure == 'connect' else httpx.ReadTimeout)('transient transport failure')

        async def __aexit__(self, *args):
            return False

    class SuccessfulContext:
        async def __aenter__(self):
            class Response:
                status_code = 200

                async def aiter_lines(self):
                    yield 'data: ' + json.dumps({'choices': [{'delta': {'content': 'ok'}, 'finish_reason': 'stop'}]})
                    yield 'data: [DONE]'
            return Response()

        async def __aexit__(self, *args):
            return False

    class Client(_Client):
        def stream(self, method, url, **kwargs):
            payloads.append(kwargs['json'])
            return FailedContext() if len(payloads) == 1 else SuccessfulContext()

    monkeypatch.setattr(core, '_get_http_client', lambda: Client())
    monkeypatch.setattr(core, '_ollama_model_caps', lambda *args: None)
    monkeypatch.setattr(core, '_is_host_dead', lambda url: False)
    monkeypatch.setattr(core, '_mark_host_dead', lambda url: False)
    monkeypatch.setattr(core, '_clear_host_dead', lambda *args: None)
    monkeypatch.setattr(core, 'note_model_activity', lambda *args, **kwargs: None)
    monkeypatch.setattr(core, 'get_context_length', lambda *args: 32768)
    monkeypatch.setattr(core, '_retry_delay', lambda *args, **kwargs: 0)
    overrides = {'think': True}
    if reasoning_budget is not None:
        overrides['reasoning_budget'] = reasoning_budget
    chunks = [chunk async for chunk in core.stream_llm('http://127.0.0.1:63490/v1',
        'glm-5.3-flash-nvfp4', [{'role': 'user', 'content': 'finish'}], gen_overrides=overrides, max_retries=3)]
    assert len(payloads) == 2
    assert payloads[0] == payloads[1]
    assert any('"delta": "ok"' in chunk for chunk in chunks)
    assert not any(chunk.startswith('event: error') for chunk in chunks)


def error(status=503, *, retryable=True, partial=False):
    return 'event: error\ndata: ' + json.dumps({'error': 'temporary service outage',
        'status': status, 'retryable': retryable, 'partial': partial,
        'error_class': 'http.service_unavailable'}) + '\n\n'


@pytest.mark.asyncio
async def test_sparks_recovers_after_more_than_one_transport_retry_cycle(monkeypatch):
    calls, waits = [], []
    messages = [{'role': 'user', 'content': 'finish'}, {'role': 'tool', 'tool_call_id': 'created-once', 'content': 'saved id42'}]
    monkeypatch.setattr(core, 'is_sparks_url', lambda url: url == 'http://spark/v1')

    async def source(url, model, prompt, **kwargs):
        calls.append((url, model, prompt, kwargs.get('headers')))
        if len(calls) <= 7:
            yield error()
        else:
            yield 'data: {"delta":"Completed using saved id42."}\n\n'
            yield 'data: [DONE]\n\n'

    async def wait(attempt):
        waits.append(attempt)
        await asyncio.sleep(0)

    monkeypatch.setattr(core, 'stream_llm', source)
    monkeypatch.setattr(core, '_wait_for_sparks_service', wait, raising=False)
    events = [chunk async for chunk in core.stream_llm_with_fallback(
        [('http://spark/v1', 'configured-model', {'Authorization': 'fixture'}),
         ('http://other/v1', 'other-model', {})], messages)]
    assert len(calls) == 8
    assert all(call == ('http://spark/v1', 'configured-model', messages, {'Authorization': 'fixture'}) for call in calls)
    assert waits == list(range(1, 8))
    assert not any(chunk.startswith('event: error') for chunk in events)
    assert sum('Completed using saved id42.' in chunk for chunk in events) == 1


@pytest.mark.parametrize('managed,status,retryable,partial', [
    (False, 503, True, False), (True, 401, True, False),
    (True, 400, True, False), (True, 503, False, False), (True, 503, True, True),
    (True, 504, False, False),
])
@pytest.mark.asyncio
async def test_non_service_and_explicit_terminal_errors_are_not_held(monkeypatch, managed, status, retryable, partial):
    calls = []
    monkeypatch.setattr(core, 'is_sparks_url', lambda url: managed)

    async def source(*args, **kwargs):
        calls.append(1)
        yield error(status, retryable=retryable, partial=partial)

    monkeypatch.setattr(core, 'stream_llm', source)
    events = [chunk async for chunk in core.stream_llm_with_fallback([('http://spark/v1', 'm', {})], [])]
    assert len(calls) == 1
    assert any(chunk.startswith('event: error') for chunk in events)


@pytest.mark.asyncio
async def test_stop_cancels_service_wait_without_another_attempt(monkeypatch):
    calls = []
    waiting = asyncio.Event()
    monkeypatch.setattr(core, 'is_sparks_url', lambda url: True)

    async def source(*args, **kwargs):
        calls.append(1)
        yield error()

    async def wait(attempt):
        waiting.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(core, 'stream_llm', source)
    monkeypatch.setattr(core, '_wait_for_sparks_service', wait, raising=False)

    async def consume():
        return [chunk async for chunk in core.stream_llm_with_fallback([('http://spark/v1', 'm', {})], [])]

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(waiting.wait(), .5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(calls) == 1
    finally:
        if not task.done():
            task.cancel()


@pytest.mark.asyncio
async def test_explicit_initial_deadline_still_ends_service_wait(monkeypatch):
    from src.first_token_budget import _OVERRIDE
    monkeypatch.setattr(core, 'is_sparks_url', lambda url: True)

    async def source(*args, **kwargs):
        yield error()

    async def wait(attempt):
        await asyncio.Event().wait()

    monkeypatch.setattr(core, 'stream_llm', source)
    monkeypatch.setattr(core, '_wait_for_sparks_service', wait)
    token = _OVERRIDE.set(.03)
    try:
        events = [chunk async for chunk in core.stream_llm_with_fallback([('http://spark/v1', 'm', {})], [])]
    finally:
        _OVERRIDE.reset(token)
    assert any('first_token_timeout' in chunk for chunk in events)


@pytest.mark.asyncio
async def test_no_replay_after_content_even_with_wrong_partial_flag(monkeypatch):
    calls = []
    monkeypatch.setattr(core, 'is_sparks_url', lambda url: True)

    async def source(*args, **kwargs):
        calls.append(1)
        yield 'data: {"delta":"Already delivered."}\n\n'
        yield error(partial=False)

    monkeypatch.setattr(core, 'stream_llm', source)
    events = [chunk async for chunk in core.stream_llm_with_fallback([('http://spark/v1', 'm', {})], [])]
    assert len(calls) == 1
    assert any(chunk.startswith('event: error') for chunk in events)


@pytest.mark.asyncio
async def test_failed_attempt_metadata_is_not_published_as_success(monkeypatch):
    calls = []
    monkeypatch.setattr(core, 'is_sparks_url', lambda url: True)

    async def source(*args, **kwargs):
        calls.append(1)
        yield 'data: ' + json.dumps({'type': 'backend_info', 'attempt': len(calls)}) + '\n\n'
        if len(calls) == 1:
            yield error()
        else:
            yield 'data: {"delta":"Done."}\n\n'
            yield 'data: [DONE]\n\n'

    async def wait(attempt):
        await asyncio.sleep(0)

    monkeypatch.setattr(core, 'stream_llm', source)
    monkeypatch.setattr(core, '_wait_for_sparks_service', wait)
    events = [json.loads(chunk[6:]) async for chunk in core.stream_llm_with_fallback(
        [('http://spark/v1', 'm', {})], []) if chunk.startswith('data: ') and '[DONE]' not in chunk]
    assert [e['attempt'] for e in events if e.get('type') == 'backend_info'] == [2]
    assert any(e.get('reason') == 'sparks_service_unavailable' for e in events)


@pytest.mark.asyncio
async def test_native_http_status_error_without_partial_field_recovers(monkeypatch):
    calls = []
    monkeypatch.setattr(core, 'is_sparks_url', lambda url: True)

    async def source(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            yield 'event: error\ndata: ' + json.dumps({'status': 503, 'text': 'service outage',
                'error_class': 'transport.llm_service_error', 'retryable': True}) + '\n\n'
        else:
            yield 'data: {"delta":"Recovered."}\n\n'
            yield 'data: [DONE]\n\n'

    async def wait(attempt):
        await asyncio.sleep(0)

    monkeypatch.setattr(core, 'stream_llm', source)
    monkeypatch.setattr(core, '_wait_for_sparks_service', wait)
    events = [chunk async for chunk in core.stream_llm_with_fallback([('http://spark/v1', 'm', {})], [])]
    assert len(calls) == 2
    assert any('Recovered.' in chunk for chunk in events)


@pytest.mark.asyncio
async def test_existing_abort_callback_can_end_the_wait(monkeypatch):
    calls, waits = [], []
    monkeypatch.setattr(core, 'is_sparks_url', lambda url: True)

    async def source(*args, **kwargs):
        calls.append(1)
        yield error()

    async def wait(attempt):
        waits.append(attempt)

    monkeypatch.setattr(core, 'stream_llm', source)
    monkeypatch.setattr(core, '_wait_for_sparks_service', wait)
    events = [chunk async for chunk in core.stream_llm_with_fallback([('http://spark/v1', 'm', {})], [],
                                                               abort_when=lambda: True)]
    assert len(calls) == 1 and waits == []
    assert any('sparks_service_unavailable' in chunk for chunk in events)
