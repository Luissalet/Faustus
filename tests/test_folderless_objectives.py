"""A project can have goals before choosing a filesystem workspace."""
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.middleware import require_admin
from routes import project_routes
from services import objectives as obj
from services import objective_locations as locations
from services.projects import ProjectStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(locations, 'managed_root', lambda: str(tmp_path / 'managed-objectives'))
    return ProjectStore(str(tmp_path / 'projects'))


def add(project, title='A goal without a folder'):
    return obj.apply_deltas(project, [{'op': 'ADD', 'title': title}], 'user')


def test_folderless_project_has_durable_goals_and_prompt_context(store):
    project = store.create('Research', owner='alice')
    assert add(project)['applied'][0]['id'] == 'OBJ-1'
    assert obj.load_state(project)['objectives']['OBJ-1']['title'] == 'A goal without a folder'
    assert 'A goal without a folder' in obj.objectives_block(project)
    assert Path(obj.objectives_path(project)).is_file()


def test_folder_binding_does_not_change_managed_goal_identity(store, tmp_path):
    project = store.create('Research', owner='alice')
    add(project)
    before = obj.objectives_path(project)
    workspace = tmp_path / 'work'
    workspace.mkdir()
    bound = store.update(project['id'], {'workspace': str(workspace)}, owner='alice')
    assert obj.objectives_path(bound) == before
    assert 'OBJ-1' in obj.load_state(bound)['objectives']


def test_owner_is_part_of_managed_goal_storage_scope(store):
    alice = store.create('Research', owner='alice')
    # Deliberately reuse an id to test storage containment, independently of
    # the project registry's own globally unique id generation.
    bob = dict(alice, owner='bob')
    add(alice, 'Alice goal')
    add(bob, 'Bob goal')
    assert obj.objectives_path(alice) != obj.objectives_path(bob)
    assert obj.load_state(alice)['objectives']['OBJ-1']['title'] == 'Alice goal'
    assert obj.load_state(bob)['objectives']['OBJ-1']['title'] == 'Bob goal'


def test_existing_workspace_storage_remains_compatible_until_rebinding(store, tmp_path):
    workspace = tmp_path / 'legacy'
    workspace.mkdir()
    project = store.create('Legacy', owner='alice', workspace=str(workspace))
    add(project, 'Keep legacy goal')
    original = Path(obj.objectives_path(project))
    assert original == workspace / '.odysseus' / 'objectives.jsonl'
    original_bytes = original.read_bytes()
    detached = store.update(project['id'], {'workspace': ''}, owner='alice')
    assert obj.load_state(detached)['objectives']['OBJ-1']['title'] == 'Keep legacy goal'
    assert original.read_bytes() == original_bytes, 'Rebinding must keep the original portable copy'
    assert obj.read_log(detached), 'The migration also preserves the audit trail'


def test_rebinding_to_different_folder_preserves_project_goals(store, tmp_path):
    first, second = tmp_path / 'first', tmp_path / 'second'
    first.mkdir(); second.mkdir()
    project = store.create('Bound', owner='alice', workspace=str(first))
    add(project, 'Portable goal')
    rebound = store.update(project['id'], {'workspace': str(second)}, owner='alice')
    assert obj.load_state(rebound)['objectives']['OBJ-1']['title'] == 'Portable goal'
    assert not (second / '.odysseus' / 'objectives.jsonl').exists()


def test_failed_migration_keeps_original_binding_and_goals(store, tmp_path, monkeypatch):
    workspace = tmp_path / 'legacy'
    workspace.mkdir()
    project = store.create('Legacy', owner='alice', workspace=str(workspace))
    add(project)
    original = obj.objectives_path(project)
    def unavailable(*args, **kwargs):
        raise OSError('Synthetic storage failure')
    monkeypatch.setattr(obj, 'preserve_for_rebinding', unavailable, raising=False)
    with pytest.raises(Exception):
        store.update(project['id'], {'workspace': ''}, owner='alice')
    current = store.get(project['id'], 'alice')
    assert current['workspace'] == project['workspace']
    assert obj.objectives_path(current) == original
    assert 'OBJ-1' in obj.load_state(current)['objectives']


def test_folderless_objective_http_routes_and_owner_boundary(store, monkeypatch):
    project = store.create('Research', owner='alice')
    monkeypatch.setattr(project_routes, 'get_store', lambda: store)
    monkeypatch.setattr(project_routes, 'effective_user', lambda request: request.headers.get('x-owner', 'alice'))
    app = FastAPI()
    app.include_router(project_routes.setup_project_routes())
    app.dependency_overrides[require_admin] = lambda: None
    with TestClient(app) as client:
        base = f"/api/projects/{project['id']}/objectives"
        assert client.post(base, json={'title': 'API goal'}).status_code == 200
        assert client.get(base).json()['objectives'][0]['title'] == 'API goal'
        assert client.patch(base + '/OBJ-1', json={'status': 'in_progress'}).status_code == 200
        assert client.get(base, headers={'x-owner': 'bob'}).status_code == 404


def test_failed_second_copy_does_not_activate_partial_managed_store(store, tmp_path, monkeypatch):
    workspace = tmp_path / 'source'
    workspace.mkdir()
    project = store.create('Source', owner='alice', workspace=str(workspace))
    add(project, 'Must survive a failed copy')
    original_path = obj.objectives_path(project)
    write = obj._atomic_write
    def fail_audit(path, content):
        if Path(path).name == obj.OBJECTIVES_LOG_FILENAME:
            raise OSError('Synthetic second-copy failure')
        return write(path, content)
    monkeypatch.setattr(obj, '_atomic_write', fail_audit)
    with pytest.raises(Exception):
        store.update(project['id'], {'workspace': ''}, owner='alice')
    assert obj.objectives_path(project) == original_path
    assert store.get(project['id'], 'alice')['workspace'] == project['workspace']
    monkeypatch.setattr(obj, '_atomic_write', write)
    add(project, 'Written after failed migration')
    detached = store.update(project['id'], {'workspace': ''}, owner='alice')
    assert len(obj.load_state(detached)['objectives']) == 2
    assert len(obj.read_log(detached)) == 2


def test_writers_holding_old_project_snapshot_follow_rebound_storage(store, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    workspace = tmp_path / 'source'
    workspace.mkdir()
    project = store.create('Source', owner='alice', workspace=str(workspace))
    add(project, 'Before migration')
    barrier = Barrier(9)
    def writer(index):
        barrier.wait(timeout=10)
        return add(project, f'Concurrent objective {index}')
    with ThreadPoolExecutor(max_workers=9) as pool:
        jobs = [pool.submit(writer, index) for index in range(8)]
        barrier.wait(timeout=10)
        detached = store.update(project['id'], {'workspace': ''}, owner='alice')
        assert all(job.result(timeout=20)['applied'] for job in jobs)
    assert len(obj.load_state(detached)['objectives']) == 9
    assert len(obj.read_log(detached)) == 9
    assert obj.objectives_path(project) == obj.objectives_path(detached)
