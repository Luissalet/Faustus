import shutil
import subprocess
from types import SimpleNamespace

import pytest

from src import git_invariants as git
from src.state_mirror.adapters import workspace


@pytest.mark.parametrize('value', ['a' * 40, 'B' * 64])
def test_head_is_verified_full_commit_id(tmp_path, monkeypatch, value):
    calls = []
    def run(repo, args, **kwargs):
        calls.append((repo, args, kwargs))
        return SimpleNamespace(returncode=0, stdout=value + '\n')
    monkeypatch.setattr(git, '_git', run)
    assert git.current_head(str(tmp_path), timeout=2) == value.lower()
    assert calls == [(str(tmp_path), ['rev-parse', '--verify', 'HEAD^{commit}'], {'timeout': 2})]


@pytest.mark.parametrize('value', ['', 'HEAD', 'a' * 7, 'a' * 41, 'x' * 40, 'a' * 40 + '\nwarning'])
def test_bad_git_output_is_not_a_head(tmp_path, monkeypatch, value):
    monkeypatch.setattr(git, '_git', lambda *a, **k: SimpleNamespace(returncode=0, stdout=value))
    assert git.current_head(str(tmp_path)) == ''


def test_head_failure_or_missing_workspace_is_not_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(git, '_git', lambda *a, **k: None)
    assert git.current_head(str(tmp_path)) == ''
    assert git.current_head(str(tmp_path / 'absent')) == ''
    assert 'head' not in workspace._state_from({'available': True, 'head': ''})
    assert workspace._state_from({'available': True, 'head': 'a' * 40})['head'] == 'a' * 40


def test_workspace_head_shares_original_cached_timestamp(tmp_path, monkeypatch):
    workspace.reset_cache()
    calls = []
    monkeypatch.setattr(workspace, 'read_head', lambda path: calls.append(path) or 'a' * 40)
    monkeypatch.setattr(workspace, 'read_branch', lambda path: '')
    monkeypatch.setattr(workspace, 'read_changes', lambda path: None)
    monkeypatch.setattr(workspace, 'read_checkpoints', lambda path: None)
    try:
        first = workspace.read(str(tmp_path))
        second = workspace.read(str(tmp_path))
        assert first == second
        assert first[1]['head'] == 'a' * 40
        assert len(calls) == 1
    finally:
        workspace.reset_cache()


@pytest.mark.skipif(not shutil.which('git'), reason='Git is not installed')
def test_actual_git_unborn_detached_and_worktree(tmp_path):
    # All writes are confined to this disposable test repository, never the app.
    repo = tmp_path / 'repo'
    repo.mkdir()
    def run(*args):
        return subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                               '-c', 'commit.gpgsign=false', *args], cwd=repo,
                              check=True, capture_output=True, text=True, timeout=10).stdout.strip()
    run('init')
    assert git.current_head(str(repo)) == ''
    run('commit', '--allow-empty', '-m', 'Test fixture')
    expected = run('rev-parse', 'HEAD')
    assert git.current_head(str(repo)) == expected
    run('checkout', '--detach')
    assert git.current_head(str(repo)) == expected
    linked = tmp_path / 'linked'
    run('worktree', 'add', '--detach', str(linked), 'HEAD')
    assert git.current_head(str(linked)) == expected
    assert not (repo / '.git' / 'index.lock').exists()
