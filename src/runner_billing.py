"""Per-run official-client authentication selection. Never reads credential files.

The existing runner remains the process supervisor. This module selects a route,
probes the official client's status, and refuses ambiguity before sending a task.
API endpoints elsewhere in Faustus remain independent of subscription clients.
"""
from __future__ import annotations

import asyncio
import json
import shutil

from src.runner_connections import AUTH_COMMANDS, _capture, parse_status


def prepare(key: str, mode: str, argv: list[str], env: dict[str, str], *, endpoint=None):
    if key not in AUTH_COMMANDS or mode not in {'subscription', 'api'}:
        raise ValueError('Choose subscription or API for an official Codex/Claude client')
    if endpoint:
        raise ValueError('Official-client billing selection cannot be combined with an endpoint override')
    clean = dict(env)
    if mode == 'subscription':
        # Neither an inherited key nor a provider redirect may change this run
        # to API billing. Never mutate the host environment or account files.
        forbidden = {'OPENAI_API_KEY', 'CODEX_API_KEY', 'OPENAI_BASE_URL', 'CODEX_ACCESS_TOKEN',
                     'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL',
                     'ANTHROPIC_PROFILE', 'CLAUDE_CODE_OAUTH_TOKEN'}
        clean = {k:v for k,v in clean.items() if k.upper() not in forbidden
                 and not k.upper().startswith('CLAUDE_CODE_USE_')}
    configured = list(argv)
    if key == 'codex':
        configured += ['--ignore-user-config', '-c', 'model_provider="openai"',
                       '-c', f'forced_login_method="{"chatgpt" if mode == "subscription" else "api"}"']
        # A base URL is not an API-vs-subscription choice. Custom providers keep
        # using the ordinary endpoint/legacy runner path, not this official one.
        clean = {k:v for k,v in clean.items() if k.upper() != 'OPENAI_BASE_URL'}
    else:
        settings = {}
        if '--settings' in configured:
            index = configured.index('--settings')
            settings = json.loads(configured[index+1])
            del configured[index:index+2]
        settings['forceLoginMethod'] = 'claudeai' if mode == 'subscription' else 'console'
        configured += ['--setting-sources', '', '--settings', json.dumps(settings, separators=(',', ':'))]
        clean = {k:v for k,v in clean.items() if k.upper() not in {
            'ANTHROPIC_BASE_URL', 'ANTHROPIC_PROFILE'}
            and not k.upper().startswith('CLAUDE_CODE_USE_')}
    return configured, clean


def verify(key: str, mode: str, env: dict[str, str]) -> dict:
    """Only a status command: not a login, quota-consuming request or fallback."""
    command = AUTH_COMMANDS[key]
    executable = shutil.which(command[0], path=env.get('PATH'))
    if not executable:
        raise ValueError(f'{key} is not installed on the client PATH')
    # run_task is synchronous and executes in the existing worker thread.
    code, output = asyncio.run(_capture([executable, *command[1:]], env))
    facts = parse_status(key, code, output)
    if facts.get('authenticated') is not True or facts.get('auth_method') != mode:
        raise ValueError(f'{key}: the official client did not confirm {mode} authentication. No task was sent and no billing fallback was used.')
    return {'requested': mode, 'confirmed': facts['auth_method'], 'automatic_fallback': False}
