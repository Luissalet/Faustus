import asyncio
import threading
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes import changesets_routes, dispatch_routes
from src import changesets, dispatch


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr(changesets_routes, 'require_admin', lambda request: None)
    monkeypatch.setattr(dispatch_routes, '_is_admin', lambda owner: True)
    app = FastAPI()
    @app.middleware('http')
    async def actor(request, call_next):
        request.state.current_user = 'alice'
        if request.headers.get('x-fixture-token'):
            request.state.api_token = True
            request.state.api_token_owner = request.headers.get('x-fixture-owner')
            request.state.api_token_scopes = ['agents:dispatch'] if request.headers.get('x-fixture-scope') else []
        return await call_next(request)
    app.include_router(changesets_routes.setup_changesets_routes())
    @app.get('/health')
    async def health():
        return {'ok': True}
    return app


@pytest.mark.parametrize('field', ['changes', 'verification', 'review', 'claims', 'commands'])
@pytest.mark.parametrize('value', ['invalid', 1, False, [42], [None]])
def test_malformed_report_is_a_typed_refusal_not_server_error(app, field, value):
    with TestClient(app) as client:
        response = client.post('/api/changesets/build', json={'intent': 'implement', field: value})
    assert response.status_code == 200
    assert response.json()['ok'] is False and response.json()['field'].startswith('changeset.')


@pytest.mark.parametrize('max_chars', ['invalid', {}, [], None, True, 0, -1, 400001, 1.5])
def test_diff_budget_is_validated_before_any_git_read(app, monkeypatch, max_chars):
    def unexpected(*args, **kwargs):
        pytest.fail('Invalid budget must not reach the diff reader')
    monkeypatch.setattr(changesets, 'diff_of', unexpected)
    with TestClient(app) as client:
        assert client.post('/api/changesets/diff', json={'max_chars': max_chars}).status_code == 400


@pytest.mark.parametrize('path', [42, {}, [], 'x' * 2001])
def test_diff_path_is_validated(app, path):
    with TestClient(app) as client:
        assert client.post('/api/changesets/diff', json={'path': path}).status_code == 400


@pytest.mark.parametrize('owner,headers,status', [
    ('bob', {}, 404), ('alice', {}, 200),
    ('bob', {'x-fixture-token': '1', 'x-fixture-owner': 'bob', 'x-fixture-scope': '1'}, 200),
    ('bob', {'x-fixture-token': '1', 'x-fixture-owner': 'alice', 'x-fixture-scope': '1'}, 404),
    ('bob', {'x-fixture-token': '1', 'x-fixture-owner': 'bob'}, 403),
    ('bob', {'x-fixture-token': '1', 'x-fixture-scope': '1'}, 403),
])
def test_dispatch_report_respects_job_owner_and_token_scope(app, monkeypatch, owner, headers, status):
    job = SimpleNamespace(owner=owner, workspace='')
    monkeypatch.setattr(dispatch, 'get', lambda _: job)
    compacted = []
    monkeypatch.setattr(dispatch, 'compact', lambda job: compacted.append(True) or {})
    monkeypatch.setattr(changesets, 'from_dispatch', lambda *args, **kwargs:
        changesets.build(intent='explore', title='Synthetic private report'))
    with TestClient(app) as client:
        response = client.get('/api/changesets/from-dispatch/private', headers=headers)
    assert response.status_code == status
    assert bool(compacted) == (status == 200)
    if status != 200:
        assert 'Synthetic private report' not in response.text


@pytest.mark.asyncio
async def test_diff_does_not_block_other_requests(app, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return {'ok': True, 'diff': ''}
    monkeypatch.setattr(changesets, 'diff_of', slow)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        pending = asyncio.create_task(client.post('/api/changesets/diff', json={}))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            assert (await asyncio.wait_for(client.get('/health'), timeout=.5)).status_code == 200
        finally:
            release.set()
            response = await pending
        assert response.status_code == 200


def test_report_record_count_is_bounded():
    from src.contracts import ContractError
    with pytest.raises(ContractError, match='500'):
        changesets.build(intent='explore', claims=[{'path': 'file'}] * 501)
