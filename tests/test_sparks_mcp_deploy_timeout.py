"""The MCP observer must outlive the native deployment operation, without replay."""
import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import json
import os
from pathlib import Path
import sys

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import pytest

from mcp_servers import sparks_server as sparks


@asynccontextmanager
async def private_api(delay=0.8, status=200):
    requests, pending = [], set()

    async def handle(reader, writer):
        task = asyncio.current_task()
        pending.add(task)
        try:
            header = await reader.readuntil(b'\r\n\r\n')
            lines = header.decode('ascii').split('\r\n')
            length = next((int(line.split(':', 1)[1]) for line in lines
                           if line.lower().startswith('content-length:')), 0)
            body = await reader.readexactly(length)
            requests.append({'line': lines[0], 'body': json.loads(body or b'{}')})
            await asyncio.sleep(delay)
            payload = json.dumps({'ok': status == 200, 'result': 'observed'}).encode()
            writer.write(f'HTTP/1.1 {status} Result\r\nContent-Type: application/json\r\nContent-Length: {len(payload)}\r\nConnection: close\r\n\r\n'.encode() + payload)
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()
            pending.discard(task)

    server = await asyncio.start_server(handle, '127.0.0.1', 0)
    async with server:
        try:
            yield f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}', requests
        finally:
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)


def scaled_client(monkeypatch, scale=.01):
    original = httpx.AsyncClient
    timeouts = []

    def client(**kwargs):
        timeouts.append(kwargs['timeout'])
        kwargs['timeout'] *= scale
        return original(**kwargs)

    monkeypatch.setattr(sparks.httpx, 'AsyncClient', client)
    return timeouts


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['start', 'stop'])
async def test_deploy_waits_for_real_http_response_and_posts_once(monkeypatch, action):
    # 80 simulated seconds exceed the old 40, but fit native operation + sync.
    async with private_api() as (base, requests):
        monkeypatch.setenv('FAUSTUS_INTERNAL_API_URL', base)
        timeouts = scaled_client(monkeypatch)
        result = await sparks.call_tool('sparks_deploy', {'recipe': 'private', 'action': action})
    assert json.loads(result[0].text) == {'ok': True, 'result': 'observed'}
    assert timeouts == [120 if action == 'start' else 960]
    assert requests == [{'line': 'POST /api/sparks/deploy HTTP/1.1',
                         'body': {'recipe': 'private', 'action': action}}]


@pytest.mark.asyncio
async def test_unrelated_calls_keep_existing_allowance(monkeypatch):
    async with private_api(delay=0) as (base, requests):
        monkeypatch.setenv('FAUSTUS_INTERNAL_API_URL', base)
        timeouts = scaled_client(monkeypatch)
        for name in ('sparks_status', 'sparks_sync', 'sparks_settings'):
            result = await sparks.call_tool(name, {})
            assert json.loads(result[0].text)['ok']
    assert timeouts == [40, 40, 40]
    assert len(requests) == 3


@pytest.mark.asyncio
async def test_http_error_is_preserved_without_replay(monkeypatch):
    async with private_api(delay=0, status=503) as (base, requests):
        monkeypatch.setenv('FAUSTUS_INTERNAL_API_URL', base)
        scaled_client(monkeypatch)
        result = await sparks.call_tool('sparks_deploy', {'recipe': 'private', 'action': 'stop'})
    assert json.loads(result[0].text)['status_code'] == 503
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('action', [None, [], {}, False, 'invalid'])
async def test_malformed_action_keeps_api_validation_and_single_post(monkeypatch, action):
    async with private_api(delay=0, status=422) as (base, requests):
        monkeypatch.setenv('FAUSTUS_INTERNAL_API_URL', base)
        timeouts = scaled_client(monkeypatch)
        result = await sparks.call_tool('sparks_deploy', {'recipe': 'private', 'action': action})
    assert json.loads(result[0].text)['status_code'] == 422
    assert timeouts == [40]
    assert len(requests) == 1 and requests[0]['body']['action'] == action


@pytest.mark.asyncio
async def test_caller_cancellation_propagates_without_replay(monkeypatch):
    async with private_api(delay=.6) as (base, requests):
        monkeypatch.setenv('FAUSTUS_INTERNAL_API_URL', base)
        scaled_client(monkeypatch)
        task = asyncio.create_task(sparks.call_tool('sparks_deploy', {'recipe': 'private', 'action': 'stop'}))
        async with asyncio.timeout(3):
            while not requests:
                await asyncio.sleep(.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_stdio_catalog_and_operation_use_native_bridge():
    root = Path(__file__).resolve().parents[1]
    # Only the HTTP clock is scaled. The actual stdio server and MCP client run.
    code = '''import asyncio
from mcp_servers import sparks_server as s
original = s.httpx.AsyncClient
def client(**kwargs):
    kwargs['timeout'] *= .01
    return original(**kwargs)
s.httpx.AsyncClient = client
asyncio.run(s.main())
'''
    async with private_api() as (base, requests):
        env = {**os.environ, 'FAUSTUS_INTERNAL_API_URL': base, 'FAUSTUS_TOKEN': '',
               'FAUSTUS_TOKEN_FILE': '', 'FAUSTUS_INTERNAL_TOOL_TOKEN': ''}
        params = StdioServerParameters(command=sys.executable, args=['-c', code], cwd=str(root), env=env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = {tool.name: tool.model_dump(by_alias=True) for tool in (await session.list_tools()).tools}
                assert tools['sparks_deploy']['x-timeout-s'] == 975
                assert all('x-timeout-s' not in tool for name, tool in tools.items() if name != 'sparks_deploy')
                result = await session.call_tool('sparks_deploy', {'recipe': 'private', 'action': 'stop'},
                                                 read_timeout_seconds=timedelta(seconds=8))
                assert not result.isError
                assert json.loads(result.content[0].text)['ok']
    assert len(requests) == 1
