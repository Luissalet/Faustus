"""A broken/unreadable auth file must never reopen first-run setup."""
import builtins
import json
from unittest.mock import Mock

import pytest


def manager(path, config=None):
    from core.auth import AuthManager
    value = AuthManager.__new__(AuthManager)
    value.auth_path = str(path)
    value._config = config or {}
    return value


@pytest.mark.parametrize('body', [
    '{broken', '[]', '{"users":[]}', '{"users":{"alice":null}}',
    '{"users":{"Alice":{}," alice ":{}}}', '{"users":{" ":{}}}',
])
def test_bad_existing_config_is_not_a_first_run(tmp_path, body):
    path = tmp_path / 'auth.json'
    path.write_text(body, encoding='utf-8')
    auth = manager(path)
    with pytest.raises(RuntimeError, match='authentication configuration'):
        auth._load()
    assert path.read_text(encoding='utf-8') == body


def test_failed_reload_preserves_current_users(tmp_path):
    path = tmp_path / 'auth.json'
    path.write_text('{broken', encoding='utf-8')
    previous = {'users': {'alice': {'is_admin': True}}}
    auth = manager(path, previous)
    with pytest.raises(RuntimeError):
        auth._load()
    assert auth._config is previous


def test_missing_reload_is_not_a_first_run(tmp_path):
    auth = manager(tmp_path / 'deleted.json', {'users': {'alice': {}}})
    with pytest.raises(RuntimeError):
        auth._load()
    assert 'alice' in auth._config['users']


def test_genuinely_new_install_is_allowed(tmp_path):
    auth = manager(tmp_path / 'new.json')
    auth._load()
    assert auth._config == {}


def test_permission_denied_fails_closed_after_bounded_retry(tmp_path, monkeypatch):
    from core import auth as module
    path = tmp_path / 'auth.json'
    path.write_text('{}', encoding='utf-8')
    read = Mock(side_effect=PermissionError('locked'))
    monkeypatch.setattr(builtins, 'open', read)
    monkeypatch.setattr(module.time, 'sleep', lambda _: None)
    with pytest.raises(RuntimeError, match='authentication configuration'):
        manager(path)._load()
    assert 1 <= read.call_count <= 3


def test_transient_windows_read_failure_recovers(tmp_path, monkeypatch):
    from core import auth as module
    path = tmp_path / 'auth.json'
    path.write_text(json.dumps({'users': {'Alice': {'is_admin': True}}}), encoding='utf-8')
    real_open = builtins.open
    attempts = []
    def flaky_open(*args, **kwargs):
        attempts.append(True)
        if len(attempts) == 1:
            raise PermissionError('sharing violation')
        return real_open(*args, **kwargs)
    monkeypatch.setattr(builtins, 'open', flaky_open)
    monkeypatch.setattr(module.time, 'sleep', lambda _: None)
    auth = manager(path)
    auth._load()
    assert auth._config == {'users': {'alice': {'is_admin': True}}}
    assert len(attempts) == 2
