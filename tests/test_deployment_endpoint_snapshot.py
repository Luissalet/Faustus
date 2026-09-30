"""H22 deployment snapshots: no provider HTTP or application database."""
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database as db
from src import model_identity as mi, model_load_options as options, model_calibration as calibration
from src.creator import model_explorer as explorer
import routes.model_identity_routes as routes


@pytest.fixture
def provider(monkeypatch):
    saved = {'num_ctx': 4096, 'nested': {'value': 'before'}}
    monkeypatch.setattr(options, 'resolve_for_request', lambda *a, **k: saved)
    monkeypatch.setattr(mi, '_fetch_tags', lambda root: {'models': [{'name': 'fixture', 'digest': 'sha256:fixture'}]})
    monkeypatch.setattr(mi, '_fetch_show', lambda *a: {'capabilities': ['completion'], 'details': {'family': 'llama'},
        'model_info': {'general.architecture': 'llama', 'llama.context_length': 8192}})
    monkeypatch.setattr(mi, '_fetch_version', lambda root: 'fixture-engine')
    return saved


def resolve(revision='', endpoint='ep', scope='http://fixture/api'):
    return mi.resolve_deployment('http://fixture', 'fixture', endpoint_id=endpoint,
        endpoint_revision=revision, endpoint_scope_url=scope)


def test_options_captured_before_io_nested_copy(provider, monkeypatch):
    old_tags = mi._fetch_tags
    def tags(root):
        provider['num_ctx'] = 16384
        provider['nested']['value'] = 'after'
        return old_tags(root)
    monkeypatch.setattr(mi, '_fetch_tags', tags)
    first = resolve('A')
    assert first.deployment.configuration == {'num_ctx': 4096, 'nested': {'value': 'before'}}
    second = resolve('A')
    assert second.deployment.configuration['num_ctx'] == 16384
    assert first.model_spec.model_spec_id == second.model_spec.model_spec_id
    assert first.deployment.deployment_id != second.deployment.deployment_id


def test_scoped_ids_and_legacy_exact_identity(provider):
    a, b, other = resolve('A'), resolve('B'), resolve('A', 'other')
    assert len({a.deployment.deployment_id, b.deployment.deployment_id, other.deployment.deployment_id}) == 3
    assert a.deployment.deployment_id == resolve('A').deployment.deployment_id
    assert a.model_spec.model_spec_id == b.model_spec.model_spec_id
    legacy = resolve().deployment
    assert legacy.deployment_id == resolve(endpoint='other').deployment.deployment_id
    payload = '|'.join(['ollama', 'fixture-engine', 'sha256:fixture', mi.configuration_fingerprint(legacy.configuration)])
    assert legacy.deployment_id == mi._sha256_hex(payload)


@pytest.mark.parametrize("concurrent_wal", [False, True])
def test_migration_preserves_legacy_json_evidence_and_coexists(tmp_path, provider, concurrent_wal):
    path = str(tmp_path / 'identity.db')
    legacy = resolve().deployment.to_dict()
    legacy.pop('endpoint_revision'); legacy.pop('endpoint_scope_url')
    with sqlite3.connect(path) as connection:
        # Existing identity stores use WAL; exercise concurrent additive migration.
        if concurrent_wal:
            connection.execute("PRAGMA journal_mode = WAL")
        connection.execute('CREATE TABLE deployments (deployment_id TEXT PRIMARY KEY, model_spec_id TEXT NOT NULL, endpoint_id TEXT, endpoint_url TEXT, engine_kind TEXT, engine_version TEXT, configuration_fingerprint TEXT, manifest_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)')
        connection.execute('INSERT INTO deployments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (legacy['deployment_id'], legacy['model_spec_id'], 'ep', 'http://fixture', 'ollama', 'fixture-engine', legacy['configuration_fingerprint'], json.dumps(legacy), 'old', 'old'))
    if concurrent_wal:
        with ThreadPoolExecutor(max_workers=3) as pool:
            stores = list(pool.map(lambda _: mi.ModelIdentityStore(path), range(3)))
        store = stores[0]
    else:
        store = mi.ModelIdentityStore(path)
    evidence = store.add_evidence(deployment_id=legacy['deployment_id'], capability='tools', level='probed', source='fixture')
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda _: mi.ModelIdentityStore(path), range(3)))
    assert store.get_deployment(legacy['deployment_id']) == legacy
    for revision in ('A', 'B'):
        store.upsert_deployment(resolve(revision).deployment)
    assert len(store.list_deployments()) == 3
    assert store.list_evidence(legacy['deployment_id'])[0]['evidence_id'] == evidence.evidence_id
    with sqlite3.connect(path) as connection:
        assert connection.execute('SELECT endpoint_revision FROM deployments WHERE deployment_id=?', (legacy['deployment_id'],)).fetchone()[0] is None
        assert connection.execute('SELECT endpoint_revision, endpoint_scope_url FROM deployments WHERE deployment_id=?', (resolve('A').deployment.deployment_id,)).fetchone() == ('A', 'http://fixture/api')


@pytest.mark.parametrize("configured_url", ["http://fixture/api",
    "http://user:password@fixture/api?token=secret#fragment"])
def test_route_real_sqlite_captures_revision_url_and_options_before_io(tmp_path, monkeypatch, provider, configured_url):
    engine = create_engine('sqlite:///' + str(tmp_path / 'endpoints.db'))
    db.ModelEndpoint.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(routes, 'SessionLocal', factory)
    with factory() as session:
        row = db.ModelEndpoint(id='ep', name='fixture', base_url=configured_url, api_key=None)
        session.add(row); session.commit(); revision_a = row.connection_revision
    store = mi.ModelIdentityStore(str(tmp_path / 'identity.db'))
    monkeypatch.setattr(mi, 'default_store', lambda: store)
    monkeypatch.setattr(routes.odysseus_settings, 'get_setting', lambda key, default=None: True if key == 'creator_enabled' else default)
    original_tags = mi._fetch_tags
    def tags(root):
        assert root == routes._ollama_root(configured_url)
        with factory() as session:
            row = session.get(db.ModelEndpoint, 'ep')
            row.api_key = 'synthetic-new-key'
            session.commit()
        provider['nested']['value'] = 'after'
        return original_tags(root)
    monkeypatch.setattr(mi, '_fetch_tags', tags)
    app = FastAPI(); app.include_router(routes.setup_model_identity_routes())
    app.state.auth_manager = SimpleNamespace(is_configured=True, is_admin=lambda user: user == 'root')
    class Stamp(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.current_user = 'root'
            return await call_next(request)
    app.add_middleware(Stamp)
    with TestClient(app) as client:
        response = client.get('/api/models/identity', params={'endpoint': 'ep', 'model': 'fixture'})
        assert response.status_code == 200, response.text
        first = response.json()['deployment']
        assert first['endpoint_revision'] == revision_a
        assert first['endpoint_scope_url'] == 'http://fixture/api'
        assert first['endpoint_url'] == routes._ollama_root(configured_url)
        assert first['configuration']['nested']['value'] == 'before'
        with factory() as session:
            assert session.get(db.ModelEndpoint, 'ep').connection_revision != revision_a
        assert store.get_deployment(first['deployment_id']) == first
        # Historical reader uses the saved revision even after the endpoint changed.
        monkeypatch.setattr(calibration, '_store_path', lambda data_dir=None: str(tmp_path / 'calibration.json'))
        calibration.save_scoped_tested(vendor='ollama', model_id='fixture', endpoint_id='ep',
            endpoint_revision=revision_a, protocol=calibration.NATIVE_OLLAMA_PROTOCOL,
            digest='sha256:fixture', tested={calibration.TEST_TOOL_CALLING: {'ok': True}},
            announced={'capabilities': {'tools': True}})
        view = explorer.legacy_calibration_for(store.get_model_spec(first['model_spec_id']),
            store.get_deployment(first['deployment_id']))
        assert view['tested'][calibration.TEST_TOOL_CALLING]['ok'] is True
        monkeypatch.setattr(mi, '_fetch_tags', original_tags)
        second = client.get('/api/models/identity', params={'endpoint': 'ep', 'model': 'fixture'}).json()['deployment']
        assert second['deployment_id'] != first['deployment_id']
        assert second['model_spec_id'] == first['model_spec_id']
        raw = client.get('/api/models/identity', params={'endpoint': 'http://fixture/api', 'model': 'fixture'}).json()['deployment']
        assert raw['endpoint_revision'] == ''
    engine.dispose()


@pytest.mark.parametrize('scope,revision,expected', [('http://fixture/api', 'A', True), ('http://fixture/v1', 'A', False), ('http://fixture/api', '', False), ('http://fixture/api', 'B', False)])
def test_explorer_only_persisted_scope(tmp_path, monkeypatch, provider, scope, revision, expected):
    monkeypatch.setattr(calibration, '_store_path', lambda data_dir=None: str(tmp_path / 'calibration.json'))
    calibration.save_scoped_tested(vendor='ollama', model_id='fixture', endpoint_id='ep', endpoint_revision='A',
        protocol=calibration.NATIVE_OLLAMA_PROTOCOL, digest='sha256:fixture', tested={calibration.TEST_TOOL_CALLING: {'ok': True}}, announced={'capabilities': {'tools': True}})
    legacy_key = calibration.manifest_key(vendor='ollama', model_id='fixture', digest='sha256:fixture')
    calibration.save_tested(legacy_key, {'legacy': True}, announced={'capabilities': {'tools': True}})
    result = resolve(revision, scope=scope)
    monkeypatch.setattr(routes, 'SessionLocal', lambda: pytest.fail('historical explorer must not consult current endpoint DB'))
    view = explorer.legacy_calibration_for(result.model_spec.to_dict(), result.deployment.to_dict())
    assert bool(view['tested']) is expected
    assert view['announced']['capabilities']['tools'] is True
    if not revision:
        legacy = result.deployment.to_dict(); legacy.pop('endpoint_revision'); legacy.pop('endpoint_scope_url')
        assert explorer.legacy_calibration_for(result.model_spec.to_dict(), legacy)['tested'] == {}


def test_thread_boundary_does_not_reread_saved_options(provider, monkeypatch):
    import asyncio
    calls = []
    def saved(*args, **kwargs):
        calls.append(True)
        return provider
    monkeypatch.setattr(options, 'resolve_for_request', saved)
    async def fake_thread(function, *args, **kwargs):
        provider['nested']['value'] = 'changed-before-worker'
        return function(*args, **kwargs)
    monkeypatch.setattr(asyncio, 'to_thread', fake_thread)
    result = asyncio.run(routes._to_thread_resolve('http://fixture', 'fixture', 'ep',
        endpoint_revision='A', endpoint_scope_url='http://fixture/api'))
    assert len(calls) == 1
    assert result.deployment.configuration['nested']['value'] == 'before'


def test_missing_saved_options_fails_open_once(monkeypatch, provider):
    calls = []
    def unavailable(*args, **kwargs):
        calls.append(True)
        raise RuntimeError('synthetic settings failure')
    monkeypatch.setattr(options, 'resolve_for_request', unavailable)
    result = resolve('A')
    assert len(calls) == 1
    assert result.deployment.configuration == {'num_ctx': 8192}
    assert result.deployment.endpoint_revision == 'A'


@pytest.mark.parametrize('raw,expected', [
    ('http://user:password@fixture:11434/api?token=secret#fragment', 'http://fixture:11434/api'),
    ('https://user:password@[::1]:11434/api/chat?token=secret#fragment', 'https://[::1]:11434/api/chat'),
    ('http://fixture/v1?token=secret', 'http://fixture/v1'),
    ('http://user:password@fixture:bad/api?token=secret', ''),
    ('http://user:password@[::1/api?token=secret', ''),
    ('/api?token=secret', ''),
])
def test_scope_url_sanitized_direct_resolver_and_store(tmp_path, provider, raw, expected):
    result = resolve('A', scope=raw)
    assert result.deployment.endpoint_scope_url == expected
    assert result.deployment.deployment_id == resolve('A').deployment.deployment_id
    assert result.deployment.endpoint_url == 'http://fixture'
    store = mi.ModelIdentityStore(str(tmp_path / 'scope.db'))
    store.upsert_deployment(result.deployment)
    saved = store.get_deployment(result.deployment.deployment_id)
    assert saved['endpoint_scope_url'] == expected
    with sqlite3.connect(store.db_path) as connection:
        scope, manifest = connection.execute('SELECT endpoint_scope_url, manifest_json FROM deployments').fetchone()
    assert scope == expected
    assert json.loads(manifest)['endpoint_scope_url'] == expected
    assert all(secret not in scope for secret in ('user:', 'password', 'token=', 'secret', 'fragment'))
    if expected:
        assert calibration.explicit_native_protocol(expected) == calibration.explicit_native_protocol(raw)


def test_scope_url_sanitized_route_capture_without_changing_io(tmp_path, monkeypatch, provider):
    import asyncio
    engine = create_engine('sqlite:///' + str(tmp_path / 'scope-endpoints.db'))
    db.ModelEndpoint.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(routes, 'SessionLocal', factory)
    raw = 'http://user:password@[::1]:11434/api?token=secret#fragment'
    with factory() as session:
        session.add(db.ModelEndpoint(id='ep', name='fixture', base_url=raw, api_key=None))
        session.commit()
    captured = routes._resolve_endpoint('ep')
    observed = []
    monkeypatch.setattr(mi, '_fetch_tags', lambda root: observed.append(root) or {})
    result = asyncio.run(routes._to_thread_resolve(captured['root'], 'fixture', captured['id'],
        endpoint_revision=captured['revision'], endpoint_scope_url=captured['scope_url']))
    assert observed == [captured['root']]
    assert result.deployment.endpoint_url == captured['root']
    assert result.deployment.endpoint_scope_url == 'http://[::1]:11434/api'
    assert result.deployment.endpoint_revision == captured['revision']
    assert calibration.explicit_native_protocol(result.deployment.endpoint_scope_url) == calibration.NATIVE_OLLAMA_PROTOCOL
    engine.dispose()


def test_direct_manifest_cannot_bypass_scope_sanitization(provider):
    from dataclasses import replace
    original = resolve('A').deployment
    changed = replace(original, endpoint_scope_url='http://user:password@fixture/api?token=secret#fragment')
    assert changed.to_dict()['endpoint_scope_url'] == 'http://fixture/api'
    assert changed.deployment_id == original.deployment_id
