"""H22 persistent endpoint identity; offline real ORM/routes and deferred writer."""
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from core import database as d
from routes import model_routes as mr, local_models_routes as lm
from src import model_calibration as c


@pytest.fixture
def store(tmp_path, monkeypatch):
    engine = create_engine('sqlite:///' + str(tmp_path / 'app.db'), connect_args={'check_same_thread': False})
    d.ModelEndpoint.__table__.create(engine)
    sessions = sessionmaker(bind=engine)
    with sessions() as db:
        db.add(d.ModelEndpoint(id='a', name='fixture', base_url='http://localhost:11434/api', api_key='synthetic'))
        db.commit()
    monkeypatch.setattr(mr, 'SessionLocal', sessions)
    monkeypatch.setattr(lm, 'SessionLocal', sessions)
    monkeypatch.setattr(mr, 'require_admin', lambda request: None)
    monkeypatch.setattr(lm, 'require_admin', lambda request: None)
    monkeypatch.setattr(lm, 'require_user', lambda request: None)
    monkeypatch.setattr(c, '_default_data_dir', lambda: str(tmp_path))
    monkeypatch.setattr(c, '_record_deployment_evidence', lambda *a, **kw: None)
    app = FastAPI()
    app.include_router(mr.setup_model_routes(None))
    app.include_router(lm.setup_local_models_routes())
    yield sessions, TestClient(app)
    engine.dispose()


def revision(sessions):
    with sessions() as db:
        return db.get(d.ModelEndpoint, 'a').connection_revision


def identity(rev):
    return dict(vendor='ollama', model_id='fixture', endpoint_id='a', protocol=c.NATIVE_OLLAMA_PROTOCOL, endpoint_revision=rev)


def write(rev):
    return c.save_scoped_tested(**identity(rev), tested={c.TEST_TOOL_CALLING: {'ok': True}}, announced={'capabilities': {'tools': True}})


@pytest.mark.parametrize('body', [{'base_url': 'http://localhost:11435/api'}, {'api_key': 'replacement'}, {'endpoint_kind': 'local'}])
def test_real_patch_rotates_identity_and_old_probes_do_not_apply(store, body):
    sessions, client = store
    old = revision(sessions)
    saved = write(old)
    assert saved['calibration_key'].startswith('calibration:v3:')
    assert client.patch('/api/model-endpoints/a', json=body).status_code == 200
    current = revision(sessions)
    assert current != old and len(current) == 32
    assert c.get_effective_manifest(**identity(current))['tested'] == {}
    assert c.get_effective_manifest(**identity(old))['tested']
    assert c.get_effective_manifest(**{k:v for k,v in identity(old).items() if k != 'endpoint_revision'})['tested'] == {}


def test_label_same_config_and_rollback_preserve_revision(store):
    sessions, client = store
    before = revision(sessions)
    assert client.patch('/api/model-endpoints/a', json={'name': 'renamed', 'api_key': 'synthetic'}).status_code == 200
    assert revision(sessions) == before
    with sessions() as db:
        ep = db.get(d.ModelEndpoint, 'a')
        ep.base_url = 'http://localhost:11439/api'
        db.flush()
        assert ep.connection_revision != before
        db.rollback()
    assert revision(sessions) == before


def test_orm_provider_binding_changes_revision(store):
    sessions, _ = store
    before = revision(sessions)
    with sessions() as db:
        db.get(d.ModelEndpoint, 'a').provider_auth_id = 'synthetic-auth'
        db.commit()
    assert revision(sessions) != before


def test_late_calibration_keeps_snapshot_revision_after_real_patch(store, monkeypatch):
    sessions, client = store
    before = revision(sessions)
    entered, release = threading.Event(), threading.Event()
    monkeypatch.setattr(lm, '_show', lambda *a, **kw: {'capabilities': ['tools']})
    monkeypatch.setattr(lm, '_tags', lambda *a: [])
    monkeypatch.setattr(lm, '_ps', lambda *a: [{'name': 'fixture'}])
    monkeypatch.setattr(lm, '_client_factory', lambda *a: __import__('contextlib').nullcontext(None))
    def probe(*a, **kw):
        entered.set()
        assert release.wait(5)
        return {c.TEST_TOOL_CALLING: {'ok': True}}
    monkeypatch.setattr(c, 'run_calibration', probe)
    with ThreadPoolExecutor() as executor:
        task = executor.submit(client.post, '/api/models/fixture/calibrate?endpoint_id=a')
        try:
            assert entered.wait(5)
            response = client.patch('/api/model-endpoints/a', json={'base_url': 'http://localhost:11435/api'})
            assert response.status_code == 200
        finally:
            release.set()
        result = task.result(timeout=5)
    assert result.status_code == 200, result.text
    assert result.json()['calibration_scope']['endpoint_revision'] == before
    assert c.get_effective_manifest(**identity(revision(sessions)))['tested'] == {}
    assert c.get_effective_manifest(**identity(before))['tested']


def test_migration_backfills_once_preserving_existing_revision(tmp_path, monkeypatch):
    path = tmp_path / 'old.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE model_endpoints (id TEXT PRIMARY KEY, name TEXT)')
        db.executemany('INSERT INTO model_endpoints VALUES (?, ?)', [('a', 'A'), ('b', 'B')])
    monkeypatch.setattr(d, 'DATABASE_URL', 'sqlite:///' + str(path))
    d._migrate_add_endpoint_connection_revision()
    with sqlite3.connect(path) as db:
        before = db.execute('SELECT * FROM model_endpoints ORDER BY id').fetchall()
    d._migrate_add_endpoint_connection_revision()
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT * FROM model_endpoints ORDER BY id').fetchall() == before
    assert before[0][2] != before[1][2]
    assert all(len(row[2]) == 32 for row in before)


def test_revisionless_historical_writer_is_retained_but_never_live(store):
    scope = identity('')
    saved = c.save_scoped_tested(**scope, tested={c.TEST_TOOL_CALLING: {'ok': True}}, announced={})
    key = c.calibration_key(**scope)
    assert key.startswith('calibration:v2:')
    assert c.get_manifest(key)['tested']
    assert saved['tested'] == {}


def test_unpersisted_endpoint_cannot_probe(store, monkeypatch):
    _, client = store
    monkeypatch.setattr(lm, "list_ollama_endpoints", lambda **kw: [{"id": "env", "root": "http://localhost:11434", "name": "env"}])
    monkeypatch.setattr(lm, "_show", lambda *a: pytest.fail("must not read server"))
    response = client.post("/api/models/fixture/calibrate?endpoint_id=env")
    assert response.status_code == 409


def test_missing_and_unknown_revision_never_reuse_old_v2(store):
    scope = identity("")
    c.save_scoped_tested(**scope, tested={c.TEST_TOOL_CALLING: {"ok": True}}, announced={})
    assert c.get_effective_manifest(**scope)["tested"] == {}
    assert c.get_effective_manifest(**identity("unknown"))["tested"] == {}
