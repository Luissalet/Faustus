import asyncio
import json
import sys

import pytest

from src import runner_connections as connections


@pytest.mark.parametrize('key,code,text,method,state', [
    ('codex', 0, 'Logged in using ChatGPT\n', 'subscription', 'connected'),
    ('codex', 0, 'Logged in using an API key - sk-private', 'api', 'connected'),
    ('codex', 1, 'Not logged in', 'none', 'login_required'),
    ('codex', 1, 'Logged in using ChatGPT', 'unknown', 'unverified'),
    ('codex', 0, 'Some future authentication format', 'unknown', 'unverified'),
    ('codex', 0, 'Warning: Logged in using ChatGPT', 'unknown', 'unverified'),
    ('claude', 0, '{"loggedIn":true,"authMethod":"claude.ai","email":"private@example.com"}', 'subscription', 'connected'),
    ('claude', 0, '{"loggedIn":true,"authMethod":"api_key","apiKey":"sk-secret"}', 'api', 'connected'),
    ('claude', 0, '{"loggedIn":true,"authMethod":"console"}', 'api', 'connected'),
    ('claude', 0, '{"loggedIn":true,"authMethod":"oauth"}', 'unknown', 'unverified'),
    ('claude', 1, '{"loggedIn":false}', 'none', 'login_required'),
    ('claude', 0, '{"loggedIn":"true","authMethod":"claude.ai"}', 'unknown', 'unverified'),
    ('claude', 1, '{"loggedIn":true,"authMethod":"claude.ai"}', 'unknown', 'unverified'),
    ('claude', 0, 'null', 'unknown', 'unverified'),
    ('claude', 0, '[]', 'unknown', 'unverified'),
    ('claude', 0, '{broken', 'unknown', 'unverified'),
])
def test_status_parser_never_returns_credentials_or_guesses_auth(key, code, text, method, state):
    result = connections.parse_status(key, code, text)
    assert result['auth_method'] == method and result['state'] == state
    assert set(result) == {'authenticated', 'auth_method', 'state'}
    assert 'private' not in json.dumps(result) and 'sk-secret' not in json.dumps(result)


@pytest.fixture
def isolated(monkeypatch):
    from src import agent_runners as reg
    monkeypatch.setattr(reg, 'build_env', lambda _: {'PATH': 'test-path'})
    monkeypatch.setattr(reg, 'enabled', lambda: False)
    monkeypatch.setattr(connections.shutil, 'which', lambda *a, **k: 'official-client')


@pytest.mark.asyncio
async def test_diagnostic_only_invokes_status_and_does_not_enable_runners(isolated, monkeypatch):
    calls = []
    async def capture(argv, env):
        calls.append(argv)
        return 0, 'Logged in using ChatGPT'
    monkeypatch.setattr(connections, '_capture', capture)
    result = await connections.connection_status('codex')
    assert calls == [['official-client', 'login', 'status']]
    assert result['state'] == 'connected' and result['external_runners_enabled'] is False
    assert 'quota check' in result['note']


@pytest.mark.asyncio
async def test_missing_client_and_unsupported_never_spawn(isolated, monkeypatch):
    monkeypatch.setattr(connections.shutil, 'which', lambda *a, **k: None)
    async def forbidden(*a):
        pytest.fail('No executable should start')
    monkeypatch.setattr(connections, '_capture', forbidden)
    result = await connections.connection_status('claude')
    assert result['installed'] is False and result['authenticated'] is None
    assert result['state'] == 'not_installed'
    assert (await connections.connection_status('unverified-runner'))['state'] == 'unsupported'


@pytest.mark.asyncio
async def test_environment_conflict_only_discloses_variable_names(isolated, monkeypatch):
    from src import agent_runners as reg
    monkeypatch.setattr(reg, 'build_env', lambda _: {'OPENAI_API_KEY': 'sk-private', 'PATH': 'test'})
    async def capture(*a):
        return 0, 'Logged in using ChatGPT'
    monkeypatch.setattr(connections, '_capture', capture)
    result = await connections.connection_status('codex')
    assert result['state'] == 'configuration_conflict'
    assert result['environment_override_names'] == ['OPENAI_API_KEY']
    assert 'sk-private' not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize('error,state', [(asyncio.TimeoutError('secret'), 'timeout'), (OSError('secret'), 'unverified')])
async def test_errors_never_echo_local_diagnostics(isolated, monkeypatch, error, state):
    async def capture(*a):
        raise error
    monkeypatch.setattr(connections, '_capture', capture)
    result = await connections.connection_status('claude')
    assert result['state'] == state and 'secret' not in json.dumps(result)


@pytest.mark.asyncio
async def test_capture_combines_codex_stderr_and_reaps_on_timeout(monkeypatch):
    import os
    code, text = await connections._capture([sys.executable, '-c', 'import sys; print("Logged in using ChatGPT", file=sys.stderr)'], dict(os.environ))
    assert code == 0 and connections.parse_status('codex', code, text)['auth_method'] == 'subscription'
    real = asyncio.create_subprocess_exec
    children = []
    async def tracked(*a, **kw):
        child = await real(*a, **kw)
        children.append(child)
        return child
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', tracked)
    monkeypatch.setattr(connections, 'STATUS_TIMEOUT_S', .03)
    with pytest.raises(asyncio.TimeoutError):
        await connections._capture([sys.executable, '-c', 'import time; time.sleep(30)'], dict(os.environ))
    assert children[0].returncode is not None


@pytest.mark.asyncio
async def test_capture_bounds_output_and_reaps_on_cancel(monkeypatch):
    import os
    from src.media_inspection import MediaInspectionError
    with pytest.raises(MediaInspectionError):
        await connections._capture([sys.executable, '-c', 'print("x" * 20000)'], dict(os.environ))
    real = asyncio.create_subprocess_exec
    children = []
    started = asyncio.Event()
    async def tracked(*a, **kw):
        child = await real(*a, **kw)
        children.append(child)
        started.set()
        return child
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', tracked)
    task = asyncio.create_task(connections._capture([sys.executable, '-c', 'import time; time.sleep(30)'], dict(os.environ)))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert children[0].returncode is not None


def test_connection_route_is_admin_only(isolated, monkeypatch):
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient
    from core.middleware import require_admin
    from routes.agent_runner_routes import setup_agent_runner_routes
    app = FastAPI()
    app.include_router(setup_agent_runner_routes())
    calls = []
    async def status(key):
        calls.append(key)
        return {'runner': key, 'state': 'unverified'}
    monkeypatch.setattr(connections, 'connection_status', status)
    def deny():
        raise HTTPException(403)
    app.dependency_overrides[require_admin] = deny
    with TestClient(app) as client:
        assert client.get('/api/agent-runners/codex/connection').status_code == 403
        assert calls == []
        app.dependency_overrides[require_admin] = lambda: None
        assert client.get('/api/agent-runners/codex/connection').json()['connection']['state'] == 'unverified'
        assert client.get('/api/agent-runners/other/connection').status_code == 404
        assert calls == ['codex']
