import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.middleware import require_human, INTERNAL_TOOL_HEADER, INTERNAL_TOOL_TOKEN
from routes.agent_runner_routes import setup_agent_runner_routes
from src import agent_runners as reg, runner_billing


@pytest.fixture
def app_client(monkeypatch, tmp_path):
    import core.database as database
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session, sessionmaker
    engine = create_engine(f'sqlite:///{(tmp_path / "connections.db").as_posix()}', connect_args={'check_same_thread': False})
    database.ModelEndpoint.__table__.create(engine)
    saved = []
    class TrackingSession(Session):
        def add(self, ep, **kwargs):
            saved.append(ep)
            return super().add(ep, **kwargs)
    sessions = sessionmaker(bind=engine, class_=TrackingSession, expire_on_commit=False)
    monkeypatch.setattr(database, 'SessionLocal', sessions)
    monkeypatch.setattr(reg, 'enabled', lambda: True)
    monkeypatch.setattr(runner_billing, 'verify', lambda *a: {'confirmed': 'subscription', 'automatic_fallback': False})
    app = FastAPI()
    @app.middleware('http')
    async def owner(request, call_next):
        request.state.current_user = 'alice'
        return await call_next(request)
    app.include_router(setup_agent_runner_routes())
    app.dependency_overrides[require_human] = lambda: None
    with TestClient(app) as client:
        yield client, saved
    engine.dispose()


def test_registration_is_private_text_only_and_never_returns_capability(app_client):
    client, saved = app_client
    response = client.post('/api/agent-runners/claude/model', json={'billing_mode': 'subscription'})
    assert response.status_code == 200
    ep = saved[0]
    assert ep.owner == 'alice' and ep.supports_tools is False
    assert ep.endpoint_kind == 'official-cli' and ep.model_refresh_mode == 'manual'
    assert json.loads(ep.pinned_models) == ['client-default']
    assert ep.api_key not in response.text
    assert response.json()['default_changed'] is False


@pytest.mark.parametrize('mode', ['subscription', 'api'])
def test_codex_registration_uses_same_private_capability_and_explicit_billing(app_client, monkeypatch, mode):
    from src import codex_chat
    checked = []
    monkeypatch.setattr(codex_chat, 'verify_support', lambda env: checked.append(True))
    client, saved = app_client
    response = client.post('/api/agent-runners/codex/model', json={'billing_mode': mode})
    assert response.status_code == 200 and checked == [True]
    ep = saved[0]
    assert ep.base_url.startswith(f'faustus-cli://codex/{mode}/')
    assert ep.owner == 'alice' and ep.api_key not in response.text and not ep.supports_tools
    from src.cli_model import authorize
    assert authorize(ep.base_url, 'client-default', {'Authorization': f'Bearer {ep.api_key}'}) == ('codex', mode, 'alice')


def test_codex_registration_refuses_unverified_protocol(app_client, monkeypatch):
    from src import codex_chat
    from src.cli_model import ClientModelError
    def unsupported(env):
        raise ClientModelError('Update the official client')
    monkeypatch.setattr(codex_chat, 'verify_support', unsupported)
    client, saved = app_client
    response = client.post('/api/agent-runners/codex/model', json={'billing_mode': 'subscription'})
    assert response.status_code == 400 and 'Update' in response.text and not saved


def test_cross_origin_cannot_grant_a_client_capability(app_client):
    client, saved = app_client
    response = client.post('/api/agent-runners/claude/model', json={'billing_mode': 'subscription'}, headers={'Origin': 'https://attacker.test'})
    assert response.status_code == 403 and saved == []


def test_internal_model_tool_cannot_register_client(app_client):
    client, saved = app_client
    client.app.dependency_overrides.clear()
    response = client.post('/api/agent-runners/claude/model', json={'billing_mode': 'subscription'}, headers={INTERNAL_TOOL_HEADER: INTERNAL_TOOL_TOKEN})
    assert response.status_code == 403 and saved == []


def test_disabled_execution_is_not_silently_enabled(app_client, monkeypatch):
    client, saved = app_client
    monkeypatch.setattr(reg, 'enabled', lambda: False)
    response = client.post('/api/agent-runners/claude/model', json={'billing_mode': 'subscription'})
    assert response.status_code == 409 and saved == []


@pytest.mark.parametrize('key,body', [
    ('unverified-client', {'billing_mode': 'subscription'}),
    ('claude', {'billing_mode': 'free'}),
    ('claude', {'billing_mode': 'api', 'model': '--dangerously-skip-permissions'}),
])
def test_unverified_client_or_invalid_configuration_is_rejected(app_client, key, body):
    client, saved = app_client
    response = client.post(f'/api/agent-runners/{key}/model', json=body)
    assert response.status_code == 400 and saved == []


def test_retry_reuses_private_connection_without_rotating_capability(app_client):
    client, saved = app_client
    first = client.post('/api/agent-runners/claude/model', json={'billing_mode': 'subscription'})
    capability = saved[0].api_key
    second = client.post('/api/agent-runners/claude/model', json={'billing_mode': 'subscription'})
    assert second.status_code == 200
    assert first.json()['endpoint_id'] == second.json()['endpoint_id']
    assert second.json()['reused'] is True and len(saved) == 1
    assert saved[0].api_key == capability and capability not in second.text


def test_retry_does_not_reenable_revoked_connection(app_client):
    import core.database as database
    client, saved = app_client
    first = client.post('/api/agent-runners/claude/model', json={'billing_mode': 'subscription'})
    with database.SessionLocal() as db:
        db.get(database.ModelEndpoint, first.json()['endpoint_id']).is_enabled = False
        db.commit()
    second = client.post('/api/agent-runners/claude/model', json={'billing_mode': 'subscription'})
    assert second.status_code == 409 and len(saved) == 1


def test_different_models_and_billing_modes_are_distinct(app_client):
    client, saved = app_client
    payloads = [{'billing_mode': 'subscription'}, {'billing_mode': 'api'},
                {'billing_mode': 'subscription', 'model': 'my-explicit-model'}]
    ids = {client.post('/api/agent-runners/claude/model', json=p).json()['endpoint_id'] for p in payloads}
    assert len(ids) == 3 and len(saved) == 3


def test_concurrent_registration_has_one_database_winner(app_client):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from sqlalchemy import event
    import core.database as database
    client, _ = app_client
    both_inserting = threading.Barrier(2)
    def before_insert(*args):
        both_inserting.wait(timeout=5)
    event.listen(database.ModelEndpoint, 'before_insert', before_insert)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: client.post('/api/agent-runners/claude/model',
                json={'billing_mode': 'subscription'}), range(2)))
    finally:
        event.remove(database.ModelEndpoint, 'before_insert', before_insert)
    assert [r.status_code for r in responses] == [200, 200]
    assert len({r.json()['endpoint_id'] for r in responses}) == 1
    assert sorted(r.json()['reused'] for r in responses) == [False, True]
    with database.SessionLocal() as db:
        assert db.query(database.ModelEndpoint).count() == 1
