"""Real Git regressions: read revisions cannot turn into writing options."""
import asyncio
import json
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from src import git_panel, tool_execution as te
from src.agent_tools.git_tools import GitDiffTool, GitLogTool

pytestmark = pytest.mark.skipif(not shutil.which('git'), reason='Git unavailable')


@pytest.fixture
def repo(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('USERPROFILE', str(home))
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(home / 'gitconfig'))
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    root = tmp_path / 'project'
    root.mkdir()
    def git(*args):
        return subprocess.run(['git', *args], cwd=root, capture_output=True, text=True, check=True).stdout.strip()
    git('init', '-q')
    git('symbolic-ref', 'HEAD', 'refs/heads/main')
    (root / 'a.txt').write_text('first\n')
    git('add', 'a.txt')
    git('-c', 'user.name=QA', '-c', 'user.email=qa@example.invalid', 'commit', '-qm', 'First fixture')
    (root / 'a.txt').write_text('second\n')
    git('add', 'a.txt')
    git('-c', 'user.name=QA', '-c', 'user.email=qa@example.invalid', 'commit', '-qm', 'Second fixture')
    return root


@pytest.mark.parametrize('field', ['ref', 'cursor'])
def test_typed_log_rejects_output_option_outside_workspace(repo, tmp_path, field):
    outside = tmp_path / f'{field}-outside.txt'
    token = te._active_workspace.set(str(repo))
    try:
        result = asyncio.run(GitLogTool().execute(json.dumps({field: f'--output={outside.as_posix()}'}), {}))
    finally:
        te._active_workspace.reset(token)
    assert result['exit_code'] != 0
    assert result['error_class'] == 'git.invalid_revision'
    assert not outside.exists()


@pytest.mark.parametrize('file', [None, 'a.txt'])
def test_typed_diff_rejects_output_option_outside_workspace(repo, tmp_path, file):
    outside = tmp_path / 'diff-outside.txt'
    args = {'commit': f'--output={outside.as_posix()}'}
    if file:
        args['path_in_repo'] = file
    token = te._active_workspace.set(str(repo))
    try:
        result = asyncio.run(GitDiffTool().execute(json.dumps(args), {}))
    finally:
        te._active_workspace.reset(token)
    assert result['exit_code'] != 0
    assert result['error_class'] == 'git.invalid_revision'
    assert not outside.exists()


@pytest.mark.parametrize('entry', ['log_ref', 'log_cursor', 'commit_detail', 'commit_files', 'commit_file_diff'])
def test_panel_read_services_reject_option_revisions_before_git(repo, tmp_path, entry):
    outside = tmp_path / f'{entry}-outside.txt'
    rev = f'--output={outside.as_posix()}'
    with pytest.raises(ValueError, match='revision'):
        if entry == 'log_ref':
            git_panel.log_commits(str(repo), ref=rev)
        elif entry == 'log_cursor':
            git_panel.log_commits(str(repo), cursor=rev)
        elif entry == 'commit_detail':
            git_panel.commit_detail(str(repo), rev)
        elif entry == 'commit_files':
            git_panel.commit_files(str(repo), rev)
        else:
            git_panel.commit_file_diff(str(repo), rev, 'a.txt')
    assert not outside.exists()


def test_normal_refs_ranges_pagination_and_filename_still_work(repo):
    log = git_panel.log_commits(str(repo), ref='main', limit=1)
    assert log['commits'][0]['message'] == 'Second fixture'
    page = git_panel.log_commits(str(repo), cursor=log['next_cursor'], limit=1)
    assert page['commits'][0]['message'] == 'First fixture'
    assert len(git_panel.log_commits(str(repo), ref='HEAD~1..HEAD')['commits']) == 1
    assert len(git_panel.log_commits(str(repo), ref='all')['commits']) == 2
    assert git_panel.commit_detail(str(repo), 'HEAD')['message'] == 'Second fixture'
    assert '+second' in git_panel.commit_file_diff(str(repo), 'HEAD', 'a.txt')['diff']


def test_option_like_filename_is_a_path_after_separator(repo):
    result = git_panel.commit_file_diff(str(repo), 'HEAD', '--output=ignored.txt')
    assert not (repo / 'ignored.txt').exists()
    assert result['diff'] == ''


def test_native_dispatch_still_allows_reads_but_rejects_writing_revision(repo, tmp_path, monkeypatch):
    from src.tool_capabilities import ToolRunSecurityContext
    monkeypatch.setattr(te, 'owner_is_admin_or_single_user', lambda owner: True)
    monkeypatch.setattr('src.tool_capabilities.tool_approval_mode', lambda: 'ask')
    gate = ToolRunSecurityContext(workspace=str(repo))
    gate.external_untrusted_context_seen = True
    gate.external_sources = ['read_file']
    outside = tmp_path / 'dispatch-outside.txt'
    async def invoke(ref):
        return await te.execute_tool_block(
            SimpleNamespace(tool_type='git_log', content=json.dumps({'ref': ref})),
            workspace=str(repo), security_context=gate, owner='qa', session_id='qa-git-read',
        )
    _formatted, result = asyncio.run(invoke(f'--output={outside.as_posix()}'))
    assert result['error_class'] == 'git.invalid_revision'
    assert result['exit_code'] != 0
    assert not outside.exists()
    _formatted, result = asyncio.run(invoke('main'))
    assert result['exit_code'] == 0
    assert result['commits'][0]['message'] == 'Second fixture'


@pytest.mark.parametrize('revision', ['--all', '-p', '  --output=elsewhere', 'HEAD\n--all', 'HEAD\x00', '\rHEAD'])
def test_other_option_and_control_revisions_are_rejected(revision):
    with pytest.raises(ValueError, match='revision'):
        git_panel.validate_read_revision(revision)


def test_real_panel_api_rejects_options_and_preserves_normal_reads(repo, tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes import git_routes
    from services import projects
    monkeypatch.setenv('AUTH_ENABLED', 'false')
    store = projects.ProjectStore(str(tmp_path / 'data'))
    monkeypatch.setattr(projects, '_store', store)
    monkeypatch.setattr(projects, 'get_store', lambda: store)
    monkeypatch.setattr(git_routes, 'effective_user', lambda request: 'qa')
    store.create(name='QA', folder='QA', workspace=str(repo), owner='qa', scaffold_memory=False)
    git_panel.invalidate_discovery_cache('qa')
    app = FastAPI()
    app.include_router(git_routes.setup_git_routes())
    root = f'/api/git/repos/{git_panel.compute_repo_id(str(repo))}'
    outside = tmp_path / 'api-outside.txt'
    with TestClient(app) as client:
        for field in ('ref', 'cursor'):
            response = client.get(root + '/log', params={field: f'--output={outside.as_posix()}'})
            assert response.status_code == 400
            assert response.json()['error_class'] == 'git.invalid_revision'
        for suffix in ('', '/diff'):
            response = client.get(root + '/commits/--output=api-option.txt' + suffix, params={'path': 'a.txt'})
            assert response.status_code == 400
            assert response.json()['error_class'] == 'git.invalid_revision'
        assert client.get(root + '/log', params={'ref': 'main'}).json()['commits'][0]['message'] == 'Second fixture'
        assert client.get(root + '/commits/HEAD').status_code == 200
    assert not outside.exists()
    assert not (repo / 'api-option.txt').exists()
