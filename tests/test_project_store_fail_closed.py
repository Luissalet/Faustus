import builtins
import json
from pathlib import Path

import pytest

from services.projects import ProjectError, ProjectStore


@pytest.mark.parametrize('content', ['{broken', 'null', '{}', '42', '[{"id":"kept"},null]', '{"projects":true}'])
def test_unreadable_store_never_becomes_empty_or_overwrites_recovery(tmp_path, content):
    path = tmp_path / 'projects.json'
    path.write_text(content, encoding='utf-8')
    backup = tmp_path / 'projects.json.corrupt'
    backup.write_text('existing recovery data', encoding='utf-8')
    store = ProjectStore(str(tmp_path))
    for action in [store.list, lambda: store.create('New project', folder='New project')]:
        with pytest.raises(ProjectError):
            action()
        assert store._cache is None
        assert path.read_text(encoding='utf-8') == content
        assert backup.read_text(encoding='utf-8') == 'existing recovery data'


def test_temporary_read_lock_retries_without_renaming(tmp_path, monkeypatch):
    original = ProjectStore(str(tmp_path))
    project = original.create('Existing', folder='Existing')
    real_open = builtins.open
    attempts = []
    def locked(path, *args, **kwargs):
        if str(path) == original.path:
            attempts.append(path)
            if len(attempts) < 3:
                raise PermissionError('temporary read lock')
        return real_open(path, *args, **kwargs)
    monkeypatch.setattr(builtins, 'open', locked)
    fresh = ProjectStore(str(tmp_path))
    assert fresh.list()[0]['id'] == project['id']
    assert len(attempts) == 3
    assert not (tmp_path / 'projects.json.corrupt').exists()


def test_persistent_read_failure_preserves_bytes_and_recovers_on_retry(tmp_path, monkeypatch):
    store = ProjectStore(str(tmp_path))
    project = store.create('Existing', folder='Existing')
    path = Path(store.path)
    before = path.read_bytes()
    store._cache = None
    real_open = builtins.open
    def locked(target, *args, **kwargs):
        if str(target) == store.path:
            raise PermissionError('locked')
        return real_open(target, *args, **kwargs)
    with monkeypatch.context() as scoped:
        scoped.setattr(builtins, 'open', locked)
        with pytest.raises(ProjectError):
            store.create('Replacement', folder='Replacement')
    assert path.read_bytes() == before
    assert store.list()[0]['id'] == project['id']


def test_known_missing_file_is_not_reinitialized(tmp_path):
    store = ProjectStore(str(tmp_path))
    store.create('Original', folder='Original')
    Path(store.path).unlink()
    store._cache = None
    with pytest.raises(ProjectError, match='missing'):
        store.create('Replacement', folder='Replacement')
    assert not Path(store.path).exists()


def test_failed_publication_keeps_previous_file_and_cache(tmp_path, monkeypatch):
    from core import atomic_io
    store = ProjectStore(str(tmp_path))
    original = store.create('Original', folder='Original')
    before = Path(store.path).read_bytes()
    def fail(*args, **kwargs):
        raise PermissionError('cannot replace')
    monkeypatch.setattr(atomic_io, '_finish', fail)
    with pytest.raises(PermissionError):
        store.create('Not saved', folder='Not saved')
    assert Path(store.path).read_bytes() == before
    assert [r['id'] for r in store.list()] == [original['id']]
    assert not list(tmp_path.glob('projects.json.tmp.*'))


def test_legacy_envelope_and_new_empty_store_still_work(tmp_path):
    store = ProjectStore(str(tmp_path))
    assert store.list() == []
    assert not Path(store.path).exists()
    Path(store.path).write_text(json.dumps({'projects': [{'id': 'legacy', 'name': 'Legacy'}]}), encoding='utf-8')
    store._cache = None
    assert store.list()[0]['id'] == 'legacy'
