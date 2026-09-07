"""Unavailable objective snapshots must not become false empty snapshots."""
from pathlib import Path

import pytest

from services import objectives, projects
from src.state_mirror.adapters.base import Scope
from src.state_mirror.adapters.objectives import ObjectivesAdapter
from src.state_mirror.read_health import capture_failures


@pytest.fixture
def project(tmp_path, monkeypatch):
    store = projects.ProjectStore(str(tmp_path / 'registry'))
    monkeypatch.setattr(projects, 'get_store', lambda: store)
    project = store.create('Private project', owner='alice')
    objectives.apply_deltas(project, [{'op': 'ADD', 'title': 'Keep this private'}], 'user')
    return project


def test_foreign_project_does_not_fall_back_to_a_supplied_workspace(project, tmp_path):
    workspace = {'workspace': str(tmp_path / 'fallback')}
    objectives.apply_deltas(workspace, [{'op': 'ADD', 'title': 'Do not use as fallback'}], 'user')
    scope = Scope(owner='bob', project_id=project['id'], workspace=workspace['workspace'])
    with capture_failures() as failures:
        assert ObjectivesAdapter().discover(scope) == []
    assert failures, 'An unresolved explicit project is not a successful empty read'


def test_folderless_objectives_are_observed_from_managed_storage(project):
    scope = Scope(owner='alice', project_id=project['id'])
    with capture_failures() as failures:
        rows = ObjectivesAdapter().discover(scope)
    assert not failures
    assert len(rows) == 1 and rows[0].display_name == 'Keep this private'


def test_corrupt_objective_file_is_not_recovered_by_a_strict_mirror_read(project):
    path = Path(objectives.objectives_path(project))
    path.write_bytes(b'{broken snapshot')
    with capture_failures() as failures:
        assert ObjectivesAdapter().discover(Scope(owner='alice', project_id=project['id'])) == []
    assert failures
    assert path.read_bytes() == b'{broken snapshot'
    assert not Path(str(path) + '.corrupt').exists()
