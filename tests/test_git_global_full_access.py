"""Global Full access is approval for Git, never tool enablement or validation."""
import asyncio
from collections import namedtuple
import json
import shutil
import subprocess

import pytest
from src import constants, settings, agent_git_policy, tool_execution as te, tool_capabilities as caps
from src.agent_tools import git_tools

pytestmark = pytest.mark.skipif(shutil.which('git') is None, reason='git unavailable')
Block = namedtuple('Block', 'tool_type content')

def git(repo, *args, check=True):
    result = subprocess.run(['git', *args], cwd=repo, capture_output=True, text=True)
    if check: assert result.returncode == 0, result.stderr
    return result.stdout.strip()

@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    home = tmp_path / 'home'; home.mkdir()
    monkeypatch.setenv('HOME', str(home)); monkeypatch.setenv('USERPROFILE', str(home))
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(home / 'gitconfig'))
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    for name in ('GIT_AUTHOR_NAME','GIT_AUTHOR_EMAIL','GIT_COMMITTER_NAME','GIT_COMMITTER_EMAIL'):
        monkeypatch.delenv(name, raising=False)
    data = tmp_path / 'data'; data.mkdir()
    monkeypatch.setattr(constants, 'DATA_DIR', str(data))
    monkeypatch.setattr(settings, 'SETTINGS_FILE', str(data / 'settings.json'))
    settings._invalidate_caches()
    monkeypatch.setattr(te, 'owner_is_admin_or_single_user', lambda owner: True)
    yield
    settings._invalidate_caches()

@pytest.fixture
def repo(tmp_path):
    root = tmp_path / 'repo'; root.mkdir(); git(root, 'init', '-b', 'main')
    git(root, 'config', 'user.name', 'Luissalet')
    git(root, 'config', 'user.email', 'luissalet@users.noreply.github.com')
    (root / 'sample.txt').write_text('initial\n')
    git(root, 'add', '--', 'sample.txt'); git(root, 'commit', '-m', 'Initial fixture')
    return root

def mode(value):
    if value is False:
        from pathlib import Path
        Path(settings.SETTINGS_FILE).write_text(json.dumps({'tool_approval_mode':value}))
        settings._invalidate_caches()
    else: settings.update_settings({'tool_approval_mode':value})

@pytest.mark.parametrize('tool,field', [
    ('git_commit','commit'),('git_branch','use_branch'),('git_checkout','use_branch'),
    ('git_merge','use_branch'),('git_delete_branch','use_branch'),('git_push','push'),
    ('git_publish','push'),('git_open_pr','push')])
def test_full_is_the_same_approval_for_every_overrideable_git_field(tool,field):
    mode('full')
    before = agent_git_policy.get_global_policy()
    assert git_tools._policy_denied(tool,field,{}, {},allowed=False) is None
    assert agent_git_policy.get_global_policy() == before

@pytest.mark.parametrize('value',['ask','auto','FULL',' full ',None,False,'invalid'])
def test_non_full_and_spoofed_arguments_preserve_refusal(value):
    mode(value)
    result=git_tools._policy_denied('git_commit','commit',{'tool_approval_mode':'full'}, {'tool_approval_mode':'full'},allowed=False)
    assert result['git_policy_field']=='commit' and result['exit_code']==1

def test_unavailable_settings_fail_as_ask(monkeypatch):
    def unavailable(*args): raise OSError('fixture unavailable')
    monkeypatch.setattr(caps,'get_setting',unavailable)
    assert git_tools._policy_denied('git_commit','commit',{}, {},allowed=False)['exit_code']==1

def test_full_commit_through_native_executor_without_confirmation(repo):
    mode('full'); before=agent_git_policy.get_global_policy()
    (repo/'sample.txt').write_text('changed\n')
    _, result=asyncio.run(te.execute_tool_block(Block('git_commit',json.dumps({'message':'Global approval commit','paths':['sample.txt']})),
        owner='qa-176',session_id='full',workspace=str(repo),
        security_context=caps.ToolRunSecurityContext(external_untrusted_context_seen=True)))
    assert result['exit_code']==0 and result['sha']==git(repo,'rev-parse','HEAD')
    assert git(repo,'log','-1','--format=%s')=='Global approval commit'
    assert agent_git_policy.get_global_policy()==before

@pytest.mark.parametrize('value',['ask','auto'])
def test_without_global_full_native_commit_still_refused(repo,value):
    mode(value); initial=git(repo,'rev-parse','HEAD')
    (repo/'sample.txt').write_text('changed\n')
    _, result=asyncio.run(te.execute_tool_block(Block('git_commit',json.dumps({'message':'Not approved','paths':['sample.txt']})),
        owner='qa-176',session_id=value,workspace=str(repo),security_context=caps.ToolRunSecurityContext()))
    assert result['git_policy_field']=='commit' and result['exit_code']==1
    assert git(repo,'rev-parse','HEAD')==initial

def test_disabled_tool_is_not_reenabled_by_full(repo):
    mode('full'); initial=git(repo,'rev-parse','HEAD')
    (repo/'sample.txt').write_text('changed\n')
    _, result=asyncio.run(te.execute_tool_block(Block('git_commit',json.dumps({'message':'Disabled','paths':['sample.txt']})),
        owner='qa-176',workspace=str(repo),disabled_tools={'git_commit'},security_context=caps.ToolRunSecurityContext()))
    assert result['exit_code']==1 and 'disabled' in result['error']
    assert git(repo,'rev-parse','HEAD')==initial

def test_full_still_validates_commit_paths(repo):
    mode('full'); initial=git(repo,'rev-parse','HEAD')
    _, result=asyncio.run(te.execute_tool_block(Block('git_commit',json.dumps({'message':'Unsafe path','paths':['../outside.txt']})),
        owner='qa-176',workspace=str(repo),security_context=caps.ToolRunSecurityContext()))
    assert result['exit_code']==1 and 'escapes' in result['error']
    assert git(repo,'rev-parse','HEAD')==initial

def test_full_still_requires_repo_identity(repo):
    mode('full'); git(repo,'config','--unset','user.name'); git(repo,'config','--unset','user.email')
    (repo/'sample.txt').write_text('changed\n'); initial=git(repo,'rev-parse','HEAD')
    _, result=asyncio.run(te.execute_tool_block(Block('git_commit',json.dumps({'message':'No identity','paths':['sample.txt']})),
        owner='qa-176',workspace=str(repo),security_context=caps.ToolRunSecurityContext()))
    assert result['exit_code']==1 and result['error_class']=='git.no_identity'
    assert git(repo,'rev-parse','HEAD')==initial

def test_full_checked_once_before_the_git_operation(monkeypatch):
    calls=[]
    monkeypatch.setattr(caps,'get_setting',lambda *args: calls.append(args) or 'full')
    assert git_tools._policy_denied('git_commit','commit',{}, {},allowed=False) is None
    assert len(calls)==1


