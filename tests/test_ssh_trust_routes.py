import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.middleware import INTERNAL_TOOL_HEADER, INTERNAL_TOOL_TOKEN
from routes import cookbook_routes as routes


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(routes, 'require_admin', lambda request: None)
    app = FastAPI()
    app.include_router(routes.setup_cookbook_routes())
    return TestClient(app)


@pytest.mark.parametrize('action', ['pair', 'unpair'])
def test_model_cannot_approve_ssh_host_identity(client, monkeypatch, action):
    monkeypatch.setattr(routes.ssh_trust, 'pair_host', lambda *a, **k: pytest.fail('trust store mutated'))
    monkeypatch.setattr(routes.ssh_trust, 'forget_host', lambda *a, **k: pytest.fail('trust store mutated'))
    response = client.post(f'/api/cookbook/ssh/{action}', json={'host':'gpu-box','fingerprint':'SHA256:test'}, headers={INTERNAL_TOOL_HEADER:INTERNAL_TOOL_TOKEN})
    assert response.status_code == 403


@pytest.mark.parametrize('action', ['pair', 'unpair'])
def test_cross_origin_trust_changes_rejected(client, monkeypatch, action):
    monkeypatch.setattr(routes, 'require_human', lambda request: None)
    response = client.post(f'/api/cookbook/ssh/{action}', json={'host':'gpu-box','fingerprint':'SHA256:test'}, headers={'Origin':'https://attacker.test'})
    assert response.status_code == 403


def test_ssh_timeout_reaps_its_process(client, monkeypatch):
    from src.agent_tools import subprocess_tools
    killed = []
    class Proc:
        returncode = None
        async def communicate(self): raise asyncio.TimeoutError()
        async def wait(self): self.returncode = -1; return -1
    proc = Proc()
    async def spawn(*a, **k): return proc
    async def kill(p): killed.append(p)
    monkeypatch.setattr(routes.asyncio, 'create_subprocess_exec', spawn)
    monkeypatch.setattr(subprocess_tools, '_kill_tree_async', kill)
    response = client.post('/api/cookbook/test-ssh', json={'host':'gpu-box'})
    assert response.status_code == 200 and response.json()['exit_code'] == 124
    assert killed == [proc] and proc.returncode == -1
