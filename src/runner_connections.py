"""Read-only official-client connection diagnostics, never credential extraction.

Only invokes `codex login status` / `claude auth status --json`. The answer
describes the CLI's current configuration, not a billing guarantee for a future
task. In particular it must never advertise a detected executable as connected.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess

STATUS_TIMEOUT_S = 8
MAX_STATUS_BYTES = 16384
AUTH_COMMANDS = {'codex': ('codex', 'login', 'status'),
                 'claude': ('claude', 'auth', 'status', '--json')}
LOGIN_COMMANDS = {'codex': 'codex login', 'claude': 'claude auth login'}
DOCS = {'codex': 'https://learn.chatgpt.com/docs/auth',
        'claude': 'https://code.claude.com/docs/en/authentication'}


def parse_status(key, returncode, text):
    """Allowlisted facts only: discard email, organizations, tokens and raw logs."""
    unknown = {'authenticated': None, 'auth_method': 'unknown', 'state': 'unverified'}
    if key == 'claude':
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            return unknown
        if not isinstance(data, dict):
            return unknown
        if data.get('loggedIn') is False:
            return {'authenticated': False, 'auth_method': 'none', 'state': 'login_required'}
        if data.get('loggedIn') is not True or returncode != 0:
            return unknown
        method = data.get('authMethod')
        # Future values remain unknown rather than guessing that OAuth always
        # means subscription (Console OAuth can be API usage).
        if method == 'claude.ai':
            method = 'subscription'
        elif method in {'api_key', 'apiKey', 'console'}:
            method = 'api'
        else:
            method = 'unknown'
        return {'authenticated': True, 'auth_method': method, 'state': 'connected' if method != 'unknown' else 'unverified'}
    if key == 'codex':
        lines = [line.strip() for line in str(text).splitlines()]
        if returncode == 0 and 'Logged in using ChatGPT' in lines:
            return {'authenticated': True, 'auth_method': 'subscription', 'state': 'connected'}
        if returncode == 0 and any(line == 'Logged in using an API key' or line.startswith('Logged in using an API key - ') for line in lines):
            return {'authenticated': True, 'auth_method': 'api', 'state': 'connected'}
        if returncode != 0 and 'Not logged in' in lines:
            return {'authenticated': False, 'auth_method': 'none', 'state': 'login_required'}
    return unknown


async def _capture(command, env):
    """Bound both pipes while reading, and reap on timeout/cancellation/overflow."""
    from src.media_inspection import _read_bounded
    spawn = asyncio.create_task(asyncio.create_subprocess_exec(*command, env=env,
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        **({'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt'
           else {'start_new_session': True})))
    try:
        proc = await asyncio.shield(spawn)
    except asyncio.CancelledError:
        proc = await spawn
        await _reap_status_process(proc)
        raise
    readers = [asyncio.create_task(_read_bounded(proc.stdout, MAX_STATUS_BYTES)),
               asyncio.create_task(_read_bounded(proc.stderr, MAX_STATUS_BYTES))]
    async def collect():
        stdout, stderr = await asyncio.gather(*readers)
        await proc.wait()
        # Codex reports status on stderr in current clients.
        return proc.returncode, (stdout + b'\n' + stderr).decode('utf-8', errors='replace')
    try:
        return await asyncio.wait_for(collect(), STATUS_TIMEOUT_S)
    finally:
        for reader in readers:
            reader.cancel()
        await _reap_status_process(proc)
        await asyncio.gather(*readers, return_exceptions=True)


async def _reap_status_process(proc):
    from src.agent_tools.subprocess_tools import _kill_tree_async
    if proc.returncode is None:
        await _kill_tree_async(proc)
    await asyncio.wait_for(proc.wait(), timeout=5)


async def connection_status(key):
    from src import agent_runners as reg
    if key not in AUTH_COMMANDS:
        return {'runner': key, 'state': 'unsupported', 'authenticated': None, 'auth_method': 'unknown',
                'note': 'No verified authentication diagnostic is available for this runner.'}
    runner = reg.get(key, help_source='')
    env = reg.build_env(runner)
    argv = AUTH_COMMANDS[key]
    executable = shutil.which(argv[0], path=env.get('PATH')) or shutil.which(argv[0])
    result = {'runner': key, 'installed': bool(executable), 'authenticated': None, 'auth_method': 'unknown',
              'external_runners_enabled': reg.enabled(), 'login_command': LOGIN_COMMANDS[key],
              'documentation_url': DOCS[key], 'state': 'not_installed',
              'note': 'Snapshot of the official client account configuration, not a model request, quota check or guarantee of future billing. Faustus issued only a status command, not a login, logout or installation command.'}
    # Names only, never values. These can change which provider/auth the client
    # selects. Saved client settings can also override auth; do not call this
    # list exhaustive or infer the active method from environment alone.
    names = {'OPENAI_API_KEY', 'CODEX_API_KEY', 'OPENAI_BASE_URL', 'CODEX_ACCESS_TOKEN'} if key == 'codex' else {
        'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL', 'CLAUDE_CODE_USE_BEDROCK',
        'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY', 'ANTHROPIC_PROFILE', 'CLAUDE_CODE_OAUTH_TOKEN'}
    result['environment_override_names'] = sorted(k for k, v in env.items() if k.upper() in names and v)
    if not executable:
        return result
    try:
        code, text = await _capture([executable, *argv[1:]], env)
        result.update(parse_status(key, code, text))
    except asyncio.TimeoutError:
        result['state'] = 'timeout'
    except Exception:
        result['state'] = 'unverified'
    # A saved ChatGPT account does not prove the next exec ignores a per-run
    # key or endpoint. Surface the mismatch instead of advertising subscription.
    if result['auth_method'] == 'subscription' and result['environment_override_names']:
        result['state'] = 'configuration_conflict'
    return result
