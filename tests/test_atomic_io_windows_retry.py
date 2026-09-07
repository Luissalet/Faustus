"""Windows sharing violations may interrupt an otherwise atomic replacement."""
import json

import pytest

from core import atomic_io


def locked():
    error = PermissionError('temporary sharing violation')
    error.winerror = 32
    return error


@pytest.mark.parametrize('kind', ['json', 'text'])
def test_transient_replace_failure_reuses_complete_temp_file(tmp_path, monkeypatch, kind):
    path = tmp_path / 'config'
    path.write_text('old', encoding='utf-8')
    original = atomic_io.os.replace
    attempts = []
    def replace(source, target):
        attempts.append(source)
        if len(attempts) < 3:
            raise locked()
        original(source, target)
    monkeypatch.setattr(atomic_io.os, 'replace', replace)
    if kind == 'json':
        atomic_io.atomic_write_json(str(path), {'new': True})
        assert json.loads(path.read_text(encoding='utf-8')) == {'new': True}
    else:
        atomic_io.atomic_write_text(str(path), 'new')
        assert path.read_text(encoding='utf-8') == 'new'
    assert len(attempts) == 3
    assert len(set(attempts)) == 1, 'retry replacement, not creation or content generation'
    assert list(tmp_path.iterdir()) == [path]


def test_persistent_lock_is_bounded_preserves_original_and_cleans_temp(tmp_path, monkeypatch):
    path = tmp_path / 'config'
    path.write_text('old', encoding='utf-8')
    attempts = []
    def replace(*_):
        attempts.append(True)
        raise locked()
    monkeypatch.setattr(atomic_io.os, 'replace', replace)
    with pytest.raises(PermissionError):
        atomic_io.atomic_write_text(str(path), 'new')
    assert 1 < len(attempts) <= 6
    assert path.read_text(encoding='utf-8') == 'old'
    assert list(tmp_path.iterdir()) == [path]


def test_non_windows_permission_failure_is_not_retried(tmp_path, monkeypatch):
    attempts = []
    def replace(*_):
        attempts.append(True)
        raise PermissionError('not a Windows sharing error')
    monkeypatch.setattr(atomic_io.os, 'replace', replace)
    with pytest.raises(PermissionError):
        atomic_io.atomic_write_text(str(tmp_path / 'config'), 'new')
    assert len(attempts) == 1
