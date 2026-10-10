"""Literal shell authority, durable transcript and sealed one-use approvals."""
import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import shell_commands as shell
from src.tool_approvals import ToolApprovalStore
from src.tool_capabilities import ToolRunSecurityContext, capabilities_for_action


@pytest.fixture
def env(tmp_path, monkeypatch):
    state = SimpleNamespace(sessions={}, calls=[], project={}, disabled=[], need_approval=False, no_flag=False)

    class Manager:
        def get_session(self, sid):
            return state.sessions.get(sid)

        def create_session(self, sid, name, endpoint_url, model, **kw):
            assert endpoint_url == model == ""  # no configured model is needed
            session = SimpleNamespace(id=sid, name=name, owner=kw.get("owner"), history=[],
                                      project_id=kw.get("project_id"))
            state.sessions[sid] = session
            return session

        def add_message(self, sid, message):
            state.sessions[sid].history.append(message)

    async def execute(block, **kw):
        state.calls.append((block.content, kw))
        if kw['tool_policy'].blocks_action('bash', block.content):
            return '', {'blocked': True, 'exit_code': 1, 'error': 'Tool policy refused.'}
        claim = kw.get('exact_approval')
        if state.need_approval and claim is None:
            if state.no_flag:
                return '', {'blocked': True, 'exit_code': 1, 'error': 'Needs approval.',
                            'policy': 'external_untrusted_context'}
            return '', {'blocked': True, 'exit_code': 1, 'error': 'Needs approval.', 'approval_required': True}
        if claim is not None:
            assert claim.claim(owner=kw['owner'], session_id=kw['session_id'], tool_name='bash',
                               content=block.content, workspace=kw['workspace'])
        return '', {'output': 'fixture output', 'exit_code': 0,
                    'execution_target': {'kind': 'container', 'cwd': kw['workspace'], 'shell': 'bash'}}

    manager = Manager()
    monkeypatch.setattr(shell, '_manager', lambda: manager)
    monkeypatch.setattr(shell, 'execute_tool_block', execute)
    monkeypatch.setattr(shell, 'tool_approval_store', ToolApprovalStore())
    monkeypatch.setattr('services.projects.project_for_session', lambda *a: state.project)
    monkeypatch.setattr('src.settings.get_user_setting', lambda *a: state.disabled)
    state.workspace = str(tmp_path)
    state.manager = manager
    return state


def run(coro):
    return asyncio.run(coro)


def test_without_model_uses_normal_executor_and_persists_untrusted_output(env):
    result = run(shell.run_command('pwd', owner='alice', workspace=env.workspace))
    session = env.sessions[result['session_id']]
    assert [m.role for m in session.history] == ['user', 'assistant']
    assert session.history[0].content == '!!pwd'
    assert session.history[1].metadata['tool_events'][0]['output'] == 'fixture output'
    assert env.calls[0][1]['workspace'] == env.workspace
    security = ToolRunSecurityContext()
    security.observe_messages([session.history[1].to_dict()])
    assert security.external_untrusted_context_seen
    # The reply itself shows the output (not only the collapsed tool card).
    reply = session.history[1].content
    assert reply.startswith('`$ pwd` · exit 0') and 'fixture output' in reply


def test_an_unknown_session_is_404_with_the_real_managers_keyerror(env, monkeypatch):
    def raising(sid):
        raise KeyError(sid)

    monkeypatch.setattr(env.manager, 'get_session', raising)
    with pytest.raises(shell.ShellCommandError) as caught:
        shell.pending_for_session('nope', 'alice')
    assert caught.value.status == 404
    with pytest.raises(shell.ShellCommandError) as caught:
        run(shell.run_command('pwd', owner='alice', session_id='nope'))
    assert caught.value.status == 404


def test_a_long_output_shows_its_tail_in_the_reply(env, monkeypatch):
    long = 'x' * 10000 + 'END'

    async def execute(block, **kw):
        return '', {'output': long, 'exit_code': 0}

    monkeypatch.setattr(shell, 'execute_tool_block', execute)
    result = run(shell.run_command('seq', owner='alice', workspace=env.workspace))
    msg = env.sessions[result['session_id']].history[1]
    assert msg.content.rstrip().endswith('END\n```') and len(msg.content) < 6200
    assert msg.metadata['tool_events'][0]['output'] == long


def _check_literal_reply(payload):
    root = Path(__file__).resolve().parent.parent
    if not shutil.which('node') or not (root / 'node_modules/esbuild/lib/main.js').exists():
        pytest.skip('node/esbuild needed for the actual Studio reader')
    result = subprocess.run(['node', str(root / 'tests/literal_shell_fence.check.mjs')],
                            input=json.dumps(payload) if payload else '', cwd=root,
                            capture_output=True, text=True, encoding='utf-8', timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize('output', [
    '```\n# literal heading\n```\n',
    '````\n# literal heading\n````\n',
    '`````````\n- literal list\n```\n',
    '~~~~\n| literal | table |\n~~~~\n',
])
def test_output_containing_code_fences_stays_literal_in_studio(env, monkeypatch, output):
    async def execute(block, **kw):
        return '', {'output': output, 'exit_code': 0}

    monkeypatch.setattr(shell, 'execute_tool_block', execute)
    result = run(shell.run_command('printf', owner='alice', workspace=env.workspace))
    msg = env.sessions[result['session_id']].history[1]
    assert msg.metadata['tool_events'][0]['output'] == output
    assert msg.metadata['trusted'] is False and msg.metadata['tool_gate_untrusted'] is True
    _check_literal_reply({'reply': msg.content, 'output': output})


def test_studio_closing_fence_matches_the_opening_length_and_character():
    _check_literal_reply(None)


def test_other_owner_cannot_run_or_read_pending_command(env):
    result = run(shell.run_command('pwd', owner='alice', workspace=env.workspace))
    with pytest.raises(shell.ShellCommandError) as caught:
        run(shell.run_command('pwd', owner='bob', session_id=result['session_id']))
    assert caught.value.status == 404 and len(env.calls) == 1
    with pytest.raises(shell.ShellCommandError):
        shell.pending_for_session(result['session_id'], 'bob')


def test_explicit_no_login_operator_can_use_legacy_named_session(env, monkeypatch):
    monkeypatch.setattr('src.owner_identity.auth_disabled', lambda: True)
    first = run(shell.run_command('pwd', owner='alice'))
    env.need_approval = True
    pending = run(shell.run_command('fixture dangerous command', owner='', session_id=first['session_id']))
    assert shell.pending_for_session(first['session_id'], '')['approval_id'] == pending['approval_id']
    done = run(shell.answer_approval(pending['approval_id'], owner='', decision='approve_action'))
    assert done['result']['exit_code'] == 0 and env.calls[-1][1]['owner'] == 'alice'


def test_project_read_only_and_disabled_bash_cannot_be_relaxed(env):
    env.project = {'workspace': env.workspace, 'execution_defaults': {'autonomy_preset': 'read_only'}}
    result = run(shell.run_command('echo x', owner='alice', autonomy_preset='bounded_autonomous'))
    assert result['result']['blocked'] and result['approval_id'] is None
    env.project = {}; env.disabled = ['bash']
    result = run(shell.run_command('pwd', owner='alice'))
    assert result['result']['blocked']


def test_approval_binds_owner_command_workspace_and_is_single_use(env):
    env.need_approval = True
    result = run(shell.run_command('fixture dangerous command', owner='alice', workspace=env.workspace))
    key, sid = result['approval_id'], result['session_id']
    assert shell.pending_for_session(sid, 'alice')['command'] == 'fixture dangerous command'
    with pytest.raises(shell.ShellCommandError):
        run(shell.answer_approval(key, owner='bob', decision='approve_action'))
    done = run(shell.answer_approval(key, owner='alice', decision='approve_action'))
    assert done['result']['exit_code'] == 0
    assert shell.pending_for_session(sid, 'alice') is None
    with pytest.raises(shell.ShellCommandError):
        run(shell.answer_approval(key, owner='alice', decision='approve_action'))


def test_revoked_policy_is_rechecked_after_approval(env):
    env.need_approval = True
    result = run(shell.run_command('fixture dangerous command', owner='alice', workspace=env.workspace))
    env.disabled = ['bash']
    done = run(shell.answer_approval(result['approval_id'], owner='alice', decision='approve_action'))
    assert done['result']['blocked']


def test_dispatcher_denial_without_approval_flag_uses_the_real_gate(env):
    env.need_approval = env.no_flag = True
    result = run(shell.run_command('truncate qa-file --size=5', owner='alice', workspace=env.workspace))
    assert result['approval_id'] and result['result']['approval_required']


def test_declining_never_dispatches_the_command_again(env):
    env.need_approval = True
    result = run(shell.run_command('fixture dangerous command', owner='alice'))
    done = run(shell.answer_approval(result['approval_id'], owner='alice', decision='deny'))
    assert done['result']['blocked'] and len(env.calls) == 1


def test_a_model_approval_cannot_be_consumed_or_superseded(env):
    result = run(shell.run_command('pwd', owner='alice'))
    sid = result['session_id']
    model_pending = shell.tool_approval_store.create(owner='alice', session_id=sid,
        origin_run_id='model-run', tool_name='bash', content='model command', workspace='',
        external_untrusted_context_seen=True, capabilities=capabilities_for_action('bash', 'model command'))
    with pytest.raises(shell.ShellCommandError):
        run(shell.answer_approval(model_pending.approval_id, owner='alice', decision='approve_action'))
    env.need_approval = True
    result = run(shell.run_command('fixture dangerous command', owner='alice', session_id=sid))
    assert result['result']['blocked'] and not result['approval_id']
    assert shell.tool_approval_store.peek(model_pending.approval_id) is model_pending


def test_http_surface_requires_dispatch_admin_and_preserves_ownership(env, monkeypatch):
    from fastapi import FastAPI
    from starlette.testclient import TestClient
    import routes.dispatch_routes as dispatch_routes
    from routes.shell_command_routes import setup_shell_command_routes
    from core.authz import api_token_allowed
    monkeypatch.setattr(dispatch_routes, '_is_admin', lambda owner: owner == 'alice')
    app = FastAPI()

    @app.middleware('http')
    async def identities(request, call_next):
        request.state.current_user = request.headers.get('x-user', 'alice')
        if request.headers.get('x-token'):
            request.state.api_token = True
            request.state.api_token_owner = request.headers.get('x-user', 'alice')
            request.state.api_token_scopes = request.headers.get('x-scopes', '').split(',')
        return await call_next(request)

    app.include_router(setup_shell_command_routes())
    with TestClient(app) as client:
        assert client.post('/api/shell/command', json={'command': 'pwd'}, headers={'x-user': 'bob'}).status_code == 403
        assert client.post('/api/shell/command', json={'command': 'pwd'}, headers={'x-token': '1'}).status_code == 403
        allowed = client.post('/api/shell/command', json={'command': 'pwd'}, headers={'x-token': '1', 'x-scopes': 'agents:dispatch'})
        assert allowed.status_code == 200
        sid = allowed.json()['session_id']
        assert client.get('/api/shell/command/pending', params={'session_id': sid}).status_code == 200
        assert client.post('/api/shell/command/approval', json={'approval_id': 'x', 'decision': 'approve_action'}, headers={'x-user':'internal-tool'}).status_code == 403
    assert api_token_allowed('POST', '/api/shell/command', ['agents:dispatch'])[0]
    assert not api_token_allowed('POST', '/api/shell/command', ['sessions'])[0]
    assert not api_token_allowed('POST', '/api/shell/command-extra', ['agents:dispatch'])[0]
