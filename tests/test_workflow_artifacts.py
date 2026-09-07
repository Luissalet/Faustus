from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
import pytest

from core import database as db_mod, middleware
from src import artifact_identity as identity
from src.contracts import WorkflowDefinition
from src.contracts.workflow import WorkflowNode
from src.workflows import WorkflowEngine, WorkflowStore
from src.workflows.runtime import production_handlers
from src.workflows.artifacts import save
from tests.test_artifact_cutover import world, own_database, legacy  # noqa: F401


def flow():
    return WorkflowDefinition.parse({'id': 'report.save', 'version': '1.0.0', 'title': 'Save report',
        'nodes': [{'id': 'report', 'type': 'artifact_store',
                   'config': {'filename': 'report.md', 'content_from': 'inputs.report'}}]})


def test_real_engine_saves_and_reopens_a_report_idempotently(world):
    store = WorkflowStore()
    run_id = store.create_run(flow(), owner='alice', inputs={'report': '# My report'})['run_id']
    engine = WorkflowEngine(production_handlers(), store)
    assert engine.advance(run_id)['status'] == 'completed'
    result = store.node_runs(run_id)['report'].result
    found = identity.for_owner(result['artifact_ids'][0], owner='alice')
    assert found.run_id == run_id and found.owner == 'alice'
    from pathlib import Path
    assert Path(identity.path_for(found.id, owner='alice')).read_text(encoding='utf-8') == '# My report'
    assert engine.advance(run_id)['status'] == 'completed'
    assert identity.reference_count(found.blob_sha256) == 1


@pytest.mark.parametrize('config', [
    {'content': 'x', 'filename': '../outside.md'},
    {'content': 'x', 'filename': 'C:outside.md'},
    {'content_from': 'inputs.missing'}, {'content': {'object': 'not text'}},
    {'content': 'x' * (10 * 1024 * 1024 + 1)},
])
def test_invalid_content_does_not_publish_anything(world, config):
    node = WorkflowNode.parse({'id': 'output', 'type': 'artifact_store', 'config': config})
    with pytest.raises(ValueError):
        save(node, {'run_id': 'r1', 'owner': 'alice', 'inputs': {}})
    assert not list(world[0].iterdir())


def test_foreign_project_refused_before_writing(world, monkeypatch):
    from services import projects
    class Projects:
        def get(self, project_id, owner=None):
            return None
    monkeypatch.setattr(projects, 'get_store', lambda: Projects())
    with pytest.raises(ValueError, match='not owned'):
        save(flow().nodes[0], {'run_id': 'r1', 'owner': 'alice', 'project_id': 'foreign',
                              'inputs': {'report': 'private'}})
    assert not list(world[0].iterdir())


@pytest.fixture()
def client(world, monkeypatch):
    from routes.artifact_routes import setup_artifact_routes
    monkeypatch.setattr(middleware, 'auth_disabled', lambda: False)
    app = FastAPI()
    @app.middleware('http')
    async def auth(request: Request, call_next):
        request.state.current_user = request.headers.get('x-test-owner', 'alice')
        return await call_next(request)
    app.include_router(setup_artifact_routes())
    return TestClient(app)


@pytest.mark.parametrize('chat_owner,chat_project,allowed', [
    ('alice', 'project-a', True), ('bob', 'project-a', False),
    ('alice', 'project-b', False), (None, 'project-a', False),
])
def test_conversation_scope_is_verified_before_publication(world, monkeypatch, chat_owner, chat_project, allowed):
    from services import projects
    class Projects:
        def get(self, project_id, owner=None):
            return {'id': 'project-a', 'owner': 'alice'}
    monkeypatch.setattr(projects, 'get_store', lambda: Projects())
    with db_mod.SessionLocal() as db:
        db.add(db_mod.Session(id='chat-a', name='QA', endpoint_url='http://localhost',
                             model='local', owner=chat_owner, project_id=chat_project))
        db.commit()
    context = {'run_id': 'r1', 'owner': 'alice', 'project_id': 'project-a',
               'inputs': {'report': '# Report', 'session_id': 'chat-a'}}
    if allowed:
        result = save(flow().nodes[0], context)
        artifact = identity.for_owner(result['artifact_ids'][0], owner='alice')
        assert artifact.session_id == 'chat-a' and artifact.project_id == 'project-a'
    else:
        with pytest.raises(ValueError, match='conversation'):
            save(flow().nodes[0], context)
        assert not list(world[0].iterdir())


def test_download_works_before_after_migration_without_cross_owner_access(world, client):
    from src.artifact_migration import copy_legacy
    legacy(world, gallery=True)
    url = '/api/artifacts/old-1/download'
    before = client.get(url)
    assert before.status_code == 200 and b'useful report' in before.content
    assert before.headers['content-disposition'].startswith('attachment')
    assert before.headers['x-content-type-options'] == 'nosniff'
    assert client.get(url, headers={'x-test-owner': 'bob'}).status_code == 404
    assert copy_legacy()['created'] == 1
    assert client.get(url).content == before.content
    assert client.get('/api/artifacts').json()['artifacts'][0]['id'].startswith('occ_')
    assert client.get('/api/artifacts', headers={'x-test-owner': 'bob'}).json()['artifacts'] == []
    assert client.get('/api/artifacts', headers={'x-test-owner': ''}).status_code == 401
    assert client.get('/api/artifacts?limit=201').status_code == 400
