"""Routing metadata is scoped, safe and independent of folder spelling."""
from pathlib import Path

import pytest

from services import objective_locations as locations


@pytest.fixture(autouse=True)
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(locations, 'managed_root', lambda: str(tmp_path / 'managed'))


def test_hostile_project_names_cannot_escape_managed_root():
    project = {'id': '../../other', 'owner': 'alice/../../bob'}
    path = Path(locations.managed_dir(project))
    assert path.parent == Path(locations.managed_root())
    assert len(path.name) == 64


def test_null_empty_and_named_owners_are_different_stores():
    paths = {locations.managed_dir({'id': 'one', 'owner': owner}) for owner in (None, '', 'alice')}
    assert len(paths) == 3


def test_creating_routing_lock_does_not_activate_managed_storage(tmp_path):
    project = {'id': 'one', 'owner': 'alice', 'workspace': str(tmp_path / 'workspace')}
    with locations.routing_guard(project):
        with locations.routing_guard(dict(project)):
            assert not locations.active(project)
            assert locations.directory(project) == str(tmp_path / 'workspace' / '.odysseus')


@pytest.mark.parametrize('filename', ['routing.lock', locations.MARKER])
def test_linked_routing_metadata_is_rejected(tmp_path, filename):
    project = {'id': 'one', 'owner': 'alice'}
    base = Path(locations.managed_dir(project))
    base.mkdir(parents=True)
    original = tmp_path / 'unrelated'
    original.write_bytes(b'unchanged')
    (base / filename).hardlink_to(original)
    with pytest.raises(PermissionError):
        with locations.routing_guard(project):
            pytest.fail('Linked routing metadata must never be entered')
    assert original.read_bytes() == b'unchanged'
