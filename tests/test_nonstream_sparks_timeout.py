"""Real loopback TCP, scaled 421/300/900 timing; never a model or Hub call."""
import asyncio
from contextlib import asynccontextmanager
import json
import time

import httpx
import pytest
from fastapi import HTTPException

from src import llm_core as core
from src.first_token_budget import _OVERRIDE


@asynccontextmanager
async def backend(delay=.421, statuses=(200,)):
    requests, handlers = [], set()
    arrived = asyncio.Event()
    async def handle(reader, writer):
        task = asyncio.current_task()
        handlers.add(task)
        try:
            header = await reader.readuntil(b'\r\n\r\n')
            headers = dict(line.split(b':', 1) for line in header.split(b'\r\n')[1:] if b':' in line)
            length = int(next(v for k, v in headers.items() if k.lower() == b'content-length'))
            body = await reader.readexactly(length)
            requests.append({'first_line': header.split(b'\r\n', 1)[0].decode(),
                             'body': body, 'payload': json.loads(body)})
            arrived.set()
            await asyncio.sleep(delay)
            status = statuses[min(len(requests) - 1, len(statuses) - 1)]
            payload = {'model': 'fixture-actual', 'choices': [{'message': {'content': 'Complete local fixture reply.'}}],
                       'usage': {'prompt_tokens': 17, 'completion_tokens': 9, 'total_tokens': 26}}
            body = json.dumps(payload if status == 200 else {'error': {'message': 'temporary fixture failure'}}).encode()
            writer.write(f'HTTP/1.1 {status} Fixture\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n'.encode() + body)
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()
            handlers.discard(task)
    server = await asyncio.start_server(handle, '127.0.0.1', 0)
    url = 'http://127.0.0.1:' + str(server.sockets[0].getsockname()[1]) + '/v1'
    try:
        yield url, requests, arrived
    finally:
        server.close()
        await server.wait_closed()
        for task in tuple(handlers): task.cancel()
        await asyncio.gather(*tuple(handlers), return_exceptions=True)


@pytest.fixture
def isolated_core(monkeypatch):
    from src import llm_trace
    traces, observed, timeouts = [], [], []
    core._response_cache.clear()
    monkeypatch.setattr(core, '_model_load_defaults', lambda *a: {})
    monkeypatch.setattr(core, '_ollama_model_caps', lambda *a: None)
    monkeypatch.setattr(core, '_is_host_dead', lambda *a: False)
    monkeypatch.setattr(core, '_mark_host_dead', lambda *a: False)
    monkeypatch.setattr(core, '_clear_host_dead', lambda *a: None)
    monkeypatch.setattr(core, 'note_model_activity', lambda *a: None)
    monkeypatch.setattr(core, '_schedule_heal_on_garbage', lambda *a: None)
    monkeypatch.setattr(core, '_retry_delay', lambda *a, **k: 0)
    # Keep the real caller's 900/300-second values through resolve_timeout;
    # scale only the transport clock (that resolver intentionally returns ints).
    make_timeout = core._call_timeout
    monkeypatch.setattr(core, '_call_timeout', lambda seconds: make_timeout(seconds / 1000))
    monkeypatch.setattr(llm_trace, 'record_call', lambda **kwargs: traces.append(kwargs))
    @asynccontextmanager
    async def slot(*args): yield
    monkeypatch.setattr(core, '_local_model_slot', slot)
    post = core.httpx_post_kimi_aware_async
    async def real_post(*args, **kwargs):
        timeouts.append(kwargs['timeout'].read)
        return await post(*args, **kwargs)
    monkeypatch.setattr(core, 'httpx_post_kimi_aware_async', real_post)
    def usage(value, **kwargs): observed.append((value, kwargs))
    return traces, observed, timeouts, usage


async def call(monkeypatch, url, *, timeout=900, initial=.3, **kwargs):
    async with httpx.AsyncClient(trust_env=False) as client:
        monkeypatch.setattr(core, '_get_http_client', lambda: client)
        token = _OVERRIDE.set(initial)  # Test-only 1:1000 scale; production parser limits unchanged.
        try:
            return await core.llm_call_async(url, 'fixture-model', [{'role': 'user', 'content': 'Finish the fixture.'}],
                temperature=.7, max_tokens=128, timeout=timeout, max_retries=kwargs.pop('max_retries', 1), **kwargs)
        finally:
            _OVERRIDE.reset(token)


@pytest.mark.asyncio
async def test_complete_nonstream_reply_can_exceed_initial_budget(monkeypatch, isolated_core):
    traces, observed, timeouts, usage = isolated_core
    async with backend() as (url, requests, _):
        start = time.monotonic()
        result = await call(monkeypatch, url, return_model_metadata=True, _usage_observer=usage)
        assert time.monotonic() - start >= .421
    assert result == ('Complete local fixture reply.', 'fixture-actual')
    assert timeouts == [.9] and len(requests) == 1
    assert observed[0][0]['input_tokens'] == 17 and observed[0][0]['output_tokens'] == 9
    assert traces[-1]['transport'] == 'call' and traces[-1]['model'] == 'fixture-actual'
    assert traces[-1]['error'] is None
    payload = requests[0]['payload']
    assert payload['model'] == 'fixture-model' and payload['max_tokens'] == 128 and payload['temperature'] == .7
    assert payload['messages'] == [{'role': 'user', 'content': 'Finish the fixture.'}]
    assert 'stream' not in payload and 'initial_response_timeout_s' not in payload


@pytest.mark.asyncio
async def test_explicit_complete_response_timeout_still_applies(monkeypatch, isolated_core):
    _, observed, timeouts, _ = isolated_core
    unknown = []
    async with backend() as (url, requests, _):
        with pytest.raises(HTTPException) as raised:
            await call(monkeypatch, url, timeout=300, initial=.9,
                       on_outcome_unknown=lambda attempt, elapsed: unknown.append((attempt, elapsed)))
    assert raised.value.status_code == 504
    assert raised.value.error_class != 'first_token_timeout'
    assert timeouts == [.3] and len(requests) == 1 and not observed
    assert len(unknown) == 1 and unknown[0][0] == 1


@pytest.mark.asyncio
async def test_unmanaged_nonstream_behavior_is_unchanged(monkeypatch, isolated_core):
    _, _, timeouts, _ = isolated_core
    monkeypatch.setattr(core, '_sparks_initial_wait', lambda url: None)
    async with backend() as (url, requests, _):
        assert await call(monkeypatch, url, initial=None) == 'Complete local fixture reply.'
    assert timeouts == [.9] and len(requests) == 1


@pytest.mark.asyncio
async def test_caller_cancellation_still_cancels_complete_response(monkeypatch, isolated_core):
    traces, observed, _, _ = isolated_core
    async with backend(delay=10) as (url, requests, arrived):
        task = asyncio.create_task(call(monkeypatch, url))
        await asyncio.wait_for(arrived.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
    assert len(requests) == 1 and not observed
    assert len(traces) == 1


@pytest.mark.asyncio
async def test_caller_total_deadline_remains_independent(monkeypatch, isolated_core):
    async with backend() as (url, requests, arrived):
        task = asyncio.create_task(call(monkeypatch, url, timeout=900))
        await asyncio.wait_for(arrived.wait(), 2)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(task, timeout=.1)
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_retry_preserves_identical_payload_and_complete_timeout(monkeypatch, isolated_core):
    _, _, timeouts, _ = isolated_core
    async with backend(delay=.01, statuses=(503, 200)) as (url, requests, _):
        assert await call(monkeypatch, url, max_retries=2) == 'Complete local fixture reply.'
    assert timeouts == [.9, .9] and len(requests) == 2
    assert requests[0]['body'] == requests[1]['body']


@pytest.mark.asyncio
async def test_native_plain_chat_route_uses_real_complete_response(monkeypatch, isolated_core):
    from fastapi import FastAPI
    from tests.test_foreground_model_routing import _chat_endpoint
    async with backend(delay=.121) as (url, requests, _):
        endpoint, saved = _chat_endpoint(monkeypatch, endpoint_url=url)
        app = FastAPI()
        app.state.auth_manager = None
        app.add_api_route('/api/chat', endpoint, methods=['POST'])
        async with httpx.AsyncClient(trust_env=False) as upstream:
            monkeypatch.setattr(core, '_get_http_client', lambda: upstream)
            token = _OVERRIDE.set(.05)
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://qa.invalid') as api:
                    response = await api.post('/api/chat', json={'session': 'session-1', 'message': 'hello'})
            finally:
                _OVERRIDE.reset(token)
    assert response.status_code == 200, response.text
    assert response.json()['response'] == 'Complete local fixture reply.'
    assert response.json()['model'] == 'fixture-actual' and len(saved) == 1 and len(requests) == 1
