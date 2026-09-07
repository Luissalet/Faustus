"""Side-panel saves use content CAS even when user versions coalesce."""
from unittest.mock import MagicMock
import pytest


def test_document_saves_compare_content_and_preserve_newer_edits(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import core.database as db
    from routes.document import document_routes as routes
    engine = create_engine('sqlite:///' + str(tmp_path / 'docs.db'), connect_args={'check_same_thread': False})
    db.Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(routes, 'SessionLocal', sessions)
    monkeypatch.setattr(routes, 'get_current_user', lambda request: 'alice')
    monkeypatch.setattr(routes, '_verify_doc_owner', lambda session, doc, owner: None)
    monkeypatch.setattr(routes, '_assert_pdf_marker_upload_owned', lambda *args: None)
    with sessions() as session:
        session.add(db.Document(id='one', title='Plan', language='markdown', current_content='base', version_count=1, owner='alice', is_active=True))
        session.commit()
    app = FastAPI()
    app.include_router(routes.setup_document_routes(MagicMock()))
    with TestClient(app) as client:
        response = client.put('/api/document/one', json={'content': 'first edit', 'expected_content': 'base'})
        assert response.status_code == 200, response.text
        version = response.json()['version_count']
        assert client.put('/api/document/one', json={'content': 'second edit', 'expected_content': 'first edit'}).status_code == 200
        response = client.put('/api/document/one', json={'content': 'lost update', 'expected_content': 'first edit'})
        assert response.status_code == 409, response.text
    with sessions() as session:
        doc = session.get(db.Document, 'one')
        assert doc.current_content == 'second edit'
        assert doc.version_count == version
    engine.dispose()


def test_restore_conflict_preserves_agent_update_and_accepts_legacy_requests(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import core.database as db
    from routes.document import document_routes as routes
    engine = create_engine('sqlite:///' + str(tmp_path / 'restore.db'), connect_args={'check_same_thread': False})
    db.Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(routes, 'SessionLocal', sessions)
    monkeypatch.setattr(routes, 'get_current_user', lambda request: 'alice')
    monkeypatch.setattr(routes, '_verify_doc_owner', lambda session, doc, owner: None)
    with sessions() as session:
        session.add(db.Document(id='one', title='Plan', language='markdown', current_content='agent edit', version_count=2, owner='alice', is_active=True))
        session.add(db.DocumentVersion(id='v1', document_id='one', version_number=1, content='original', source='user'))
        session.add(db.DocumentVersion(id='v2', document_id='one', version_number=2, content='agent edit', source='agent'))
        session.commit()
    app = FastAPI()
    app.include_router(routes.setup_document_routes(MagicMock()))
    with TestClient(app) as client:
        response = client.post('/api/document/one/restore/1', json={'expected_content': 'stale base'})
        assert response.status_code == 409, response.text
        with sessions() as session:
            assert session.get(db.Document, 'one').current_content == 'agent edit'
            assert session.query(db.DocumentVersion).count() == 2
        response = client.post('/api/document/one/restore/1', json={'expected_content': 'agent edit'})
        assert response.status_code == 200, response.text
        assert response.json()['current_content'] == 'original'
        assert response.json()['version_count'] == 3
        assert client.post('/api/document/one/restore/2').status_code == 200
    engine.dispose()
