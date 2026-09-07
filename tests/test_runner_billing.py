import json
from dataclasses import replace

import pytest

from src import agent_runners as reg, external_worker as worker, runner_billing as billing


@pytest.mark.parametrize('key', ['codex', 'claude'])
def test_subscription_strips_api_overrides_without_changing_host(key):
    env = {'PATH': 'bin', 'OPENAI_API_KEY': 'private', 'ANTHROPIC_API_KEY': 'private',
           'ANTHROPIC_BASE_URL': 'http://local', 'CLAUDE_CODE_USE_VERTEX': '1',
           'CODEX_HOME': 'account-dir', 'CLAUDE_CONFIG_DIR': 'claude-account'}
    argv, clean = billing.prepare(key, 'subscription', [key, '-p'], env)
    assert 'OPENAI_API_KEY' not in clean and 'ANTHROPIC_API_KEY' not in clean
    assert 'ANTHROPIC_BASE_URL' not in clean and 'CLAUDE_CODE_USE_VERTEX' not in clean
    assert clean['CODEX_HOME'] == 'account-dir'
    assert env['OPENAI_API_KEY'] == 'private'
    assert 'private' not in ' '.join(argv)
    if key == 'codex':
        assert '--ignore-user-config' in argv
        assert 'forced_login_method="chatgpt"' in argv
    else:
        assert argv[argv.index('--setting-sources') + 1] == ''
        assert json.loads(argv[argv.index('--settings') + 1])['forceLoginMethod'] == 'claudeai'


def test_claude_billing_preserves_faustus_tool_hook():
    hook = {'hooks': {'PreToolUse': [{'matcher': '*', 'hooks': []}]}}
    argv, env = billing.prepare('claude', 'api', ['claude', '-p', '--settings', json.dumps(hook)], {'ANTHROPIC_API_KEY': 'key'})
    settings = json.loads(argv[argv.index('--settings')+1])
    assert settings['hooks'] == hook['hooks']
    assert settings['forceLoginMethod'] == 'console'
    assert argv.count('--settings') == 1
    assert env['ANTHROPIC_API_KEY'] == 'key'


@pytest.mark.parametrize('key,mode,endpoint', [('other', 'subscription', None), ('codex', 'free', None), ('claude', 'api', 'http://local')])
def test_ambiguous_route_rejected(key, mode, endpoint):
    with pytest.raises(ValueError):
        billing.prepare(key, mode, [key], {}, endpoint=endpoint)


def test_wrong_authentication_refused_before_sending_task(monkeypatch, tmp_path):
    monkeypatch.setattr(reg, 'enabled', lambda: True)
    monkeypatch.setattr(billing, 'verify', lambda *a: (_ for _ in ()).throw(ValueError('No task was sent')))
    monkeypatch.setattr(worker, '_spawn', lambda *a, **k: pytest.fail('must not start'))
    result = worker.run_task(reg.get('codex', help_source=''), 'private task', workspace=str(tmp_path), billing_mode='subscription')
    assert not result['ok'] and result['error'] == 'No task was sent'


def test_selected_mode_and_confirmation_follow_worker_result(monkeypatch, tmp_path):
    monkeypatch.setattr(reg, 'enabled', lambda: True)
    monkeypatch.setattr(billing, 'verify', lambda *a: {'requested': 'subscription', 'confirmed': 'subscription', 'automatic_fallback': False})
    def spawn(*args, **kwargs):
        assert 'forced_login_method="chatgpt"' in kwargs['argv']
        assert 'OPENAI_API_KEY' not in kwargs['full_env']
        return {'ok': False, 'error': 'quota exhausted'}
    monkeypatch.setattr(worker, '_spawn', spawn)
    result = worker.run_task(reg.get('codex', help_source=''), 'task', workspace=str(tmp_path), env={'OPENAI_API_KEY':'not sent'}, billing_mode='subscription')
    assert result['error'] == 'quota exhausted' and result['billing']['automatic_fallback'] is False


def test_status_probe_discards_account_details(monkeypatch):
    monkeypatch.setattr(billing.shutil, 'which', lambda *a, **k: 'claude')
    async def capture(argv, env):
        assert argv == ['claude', 'auth', 'status', '--json']
        return 0, json.dumps({'loggedIn': True, 'authMethod': 'claude.ai', 'email': 'private@example.test'})
    monkeypatch.setattr(billing, '_capture', capture)
    assert billing.verify('claude', 'subscription', {}) == {'requested': 'subscription', 'confirmed': 'subscription', 'automatic_fallback': False}
    with pytest.raises(ValueError, match='No task was sent'):
        billing.verify('claude', 'api', {})


def test_cancel_during_authentication_does_not_send_task(monkeypatch, tmp_path):
    cancelled = []
    monkeypatch.setattr(reg, 'enabled', lambda: True)
    def verify(*args):
        cancelled.append(True)
        return {'confirmed': 'subscription'}
    monkeypatch.setattr(billing, 'verify', verify)
    monkeypatch.setattr(worker, '_spawn', lambda *a, **k: pytest.fail('cancelled task was sent'))
    result = worker.run_task(reg.get('codex', help_source=''), 'task', workspace=str(tmp_path),
        billing_mode='subscription', should_cancel=lambda: bool(cancelled))
    assert result['status'] == 'cancelled' and result['outcome'] == 'cancelled'
