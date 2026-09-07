"""Text inference via the official Codex app-server, not its autonomous worker.

Credentials stay with Codex. Every request starts an ephemeral, environment-less
thread; Faustus remains the only executor of the conversation's textual tools.
"""
from __future__ import annotations

from functools import lru_cache
import json
import math
import os
from pathlib import Path
import queue
import shutil
import subprocess
import tempfile
import threading
import time

from src.cli_model import ClientModelError

MAX_FRAME = 2_000_000
MAX_ANSWER = 2_000_000
MAX_ITEMS = 4096
MAX_EVENTS = 100_000
PROVIDER = 'openai'
DISABLED = (
    'shell_tool', 'unified_exec', 'code_mode_host', 'multi_agent', 'hooks',
    'plugins', 'apps', 'browser_use', 'computer_use', 'image_generation',
    'memories', 'goals', 'sleep_tool', 'view_image', 'workspace_dependencies',
    'shell_snapshot', 'remote_plugin', 'skill_search',
)
INSTRUCTIONS = ('Continue the Faustus conversation supplied in the user message. '
    'Return only the next assistant response, in the requested language. '
    'Use no Codex tools, skills or interactive questions. Requests for tools '
    'must be text in the format specified by the supplied conversation; '
    'Faustus executes them and supplies their results in subsequent requests.')


def _process_options():
    return ({'creationflags': getattr(subprocess, 'CREATE_NO_WINDOW', 0)}
            if os.name == 'nt' else {'start_new_session': True})


def _stop(proc):
    from src.agent_tools.subprocess_tools import _kill_tree
    if proc.poll() is None:
        _kill_tree(proc)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


@lru_cache(maxsize=8)
def _schema_support(executable, stamp):
    # Unknown JSON fields can be silently ignored by older clients. Check the
    # installed binary's own schema before entrusting it with a conversation.
    from src.bounded_process_output import capture
    with tempfile.TemporaryDirectory(prefix='faustus-codex-schema-') as folder:
        proc = subprocess.Popen([executable, 'app-server', 'generate-json-schema',
            '--experimental', '--out', folder], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, **_process_options())
        result = capture(proc, timeout=15, stop=lambda: _stop(proc))
        if result.timed_out or proc.returncode != 0:
            raise ClientModelError('Codex protocol could not be verified. Update the official client and retry.')
        try:
            spec = json.loads((Path(folder) / 'v2' / 'ThreadStartParams.json').read_text(encoding='utf-8'))
            props = spec['properties']
            if not all(k in props for k in ('environments', 'ephemeral', 'selectedCapabilityRoots', 'dynamicTools')):
                raise ValueError('unsupported schema')
            if 'Empty disables environment access' not in props['environments'].get('description', ''):
                raise ValueError('unverified environment isolation')
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ClientModelError('This Codex version lacks verified environment-less chat. Update the official client.') from exc


def verify_support(env):
    executable = shutil.which('codex', path=env.get('PATH'))
    if not executable:
        raise ClientModelError('Codex is not installed on the client PATH. Install the official client first.')
    stat = os.stat(executable)
    _schema_support(executable, (stat.st_size, stat.st_mtime_ns, stat.st_ino))
    return executable


def _command(executable, mode, instruction_file):
    command = [executable, 'app-server', '--stdio']
    overrides = {
        'model_provider': PROVIDER, 'forced_login_method': 'chatgpt' if mode == 'subscription' else 'api',
        'approval_policy': 'never', 'sandbox_mode': 'read-only',
        'web_search': 'disabled', 'notify': [], 'project_doc_max_bytes': 0,
        'analytics.enabled': False, 'otel.log_user_prompt': False,
        'model_instructions_file': str(instruction_file),
    }
    for key, value in overrides.items():
        command.extend(['-c', key + '=' + json.dumps(value)])
    for feature in DISABLED:
        command.extend(['--disable', feature])
    return [*command, '--enable', 'skip_host_skill_discovery']


class _Rpc:
    """Bounded pipes; neither a noisy peer nor a blocked stdin stops cancellation."""
    def __init__(self, command, env, workspace, timeout, cancel):
        self.deadline = time.monotonic() + timeout
        self.cancel = cancel
        self.incoming = queue.Queue(maxsize=32)
        self.outgoing = queue.Queue(maxsize=4)
        self.failed = threading.Event()
        self.closed = threading.Event()
        self.sequence = 0
        self.proc = subprocess.Popen(command, cwd=workspace, env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, **_process_options())
        self.threads = [threading.Thread(target=f, daemon=True, name='faustus-codex-chat')
                        for f in (self._read, self._write, self._drain_errors)]
        for thread in self.threads:
            thread.start()

    def _read(self):
        try:
            while not self.closed.is_set():
                raw = self.proc.stdout.readline(MAX_FRAME + 1)
                if not raw:
                    break
                if len(raw) > MAX_FRAME or not raw.endswith(b'\n'):
                    raise ValueError('oversized or incomplete frame')
                message = json.loads(raw)
                if not isinstance(message, dict):
                    raise ValueError('invalid frame')
                while not self.closed.is_set():
                    try:
                        self.incoming.put(message, timeout=.1)
                        break
                    except queue.Full:
                        continue
        except Exception:
            self.failed.set()

    def _drain_errors(self):
        try:
            while self.proc.stderr.read(8192):
                pass  # Never expose credentials, paths or prompts in diagnostics.
        except Exception:
            if not self.closed.is_set():
                self.failed.set()

    def _write(self):
        try:
            while not self.closed.is_set():
                try:
                    message = self.outgoing.get(timeout=.1)
                except queue.Empty:
                    continue
                self.proc.stdin.write(message)
                self.proc.stdin.flush()
        except Exception:
            if not self.closed.is_set():
                self.failed.set()

    def check(self):
        if self.cancel is not None and self.cancel.is_set():
            raise ClientModelError('Codex chat cancelled')
        if time.monotonic() >= self.deadline:
            raise ClientModelError('Codex chat timed out. The client process was stopped; retry explicitly.')
        if self.failed.is_set():
            raise ClientModelError('Codex returned an invalid or oversized protocol message')

    def send(self, message):
        self.check()
        raw = (json.dumps(message, ensure_ascii=False) + '\n').encode('utf-8')
        try:
            self.outgoing.put_nowait(raw)
        except queue.Full as exc:
            raise ClientModelError('Codex is not accepting protocol messages') from exc

    def receive(self):
        while True:
            self.check()
            try:
                message = self.incoming.get(timeout=.1)
            except queue.Empty:
                if self.proc.poll() is not None:
                    raise ClientModelError('Codex stopped before completing the response. Check its sign-in and quota.')
                continue
            # A server request must never become an implicit grant or tool run.
            if 'method' in message and 'id' in message:
                raise ClientModelError('Codex requested a native tool or interaction; no permission was granted')
            return message

    def call(self, method, params, event=lambda _: None):
        self.sequence += 1
        request_id = self.sequence
        self.send({'id': request_id, 'method': method, 'params': params})
        while True:
            message = self.receive()
            if message.get('id') == request_id:
                if 'error' in message or not isinstance(message.get('result'), dict):
                    raise ClientModelError(f'Codex could not complete {method}. Check client configuration, version and quota.')
                return message['result']
            event(message)

    def close(self):
        self.closed.set()
        _stop(self.proc)
        for thread in self.threads:
            thread.join(timeout=2)
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            stream.close()


def _thread_overrides(config):
    if not isinstance(config, dict):
        raise ClientModelError('Codex configuration could not be verified')
    providers = config.get('model_providers') or {}
    # Config overrides merge tables, rather than replacing them. A custom
    # definition of the built-in provider could redirect credentials/prompts.
    if not isinstance(providers, dict) or providers.get('openai'):
        raise ClientModelError('Custom OpenAI provider overrides are not supported by the official-client chat. Use a Faustus API connection for custom endpoints.')
    servers = config.get('mcp_servers') or {}
    if not isinstance(servers, dict) or len(servers) > 256:
        raise ClientModelError('Codex MCP configuration could not be isolated')
    # An empty table DOES NOT remove inherited servers. Use nested values:
    # dotted override paths do not support quoting literal dots in server names.
    return {'mcp_servers': {name: {'enabled': False} for name in servers}}


def _infer(executable, env, workspace, prompt, model, mode, timeout, cancel):
    instruction_file = Path(workspace) / 'instructions.txt'
    instruction_file.write_text(INSTRUCTIONS, encoding='utf-8')
    rpc = _Rpc(_command(executable, mode, instruction_file), env, workspace, timeout, cancel)
    try:
        rpc.call('initialize', {'clientInfo': {'name': 'faustus_chat', 'version': '1'},
            'capabilities': {'experimentalApi': True}})
        rpc.send({'method': 'initialized'})
        config = rpc.call('config/read', {'includeLayers': False, 'cwd': workspace})
        overrides = _thread_overrides(config.get('config'))
        account = rpc.call('account/read', {'refreshToken': False}).get('account') or {}
        expected = 'chatgpt' if mode == 'subscription' else 'apiKey'
        if account.get('type') != expected:
            raise ClientModelError('Codex did not confirm the selected access method. No task was sent or billing fallback used.')
        params = {'cwd': workspace, 'ephemeral': True, 'environments': [],
            'selectedCapabilityRoots': [], 'dynamicTools': [], 'config': overrides,
            'approvalPolicy': 'never', 'sandbox': 'read-only', 'modelProvider': PROVIDER,
            'baseInstructions': INSTRUCTIONS, 'developerInstructions': INSTRUCTIONS}
        if model != 'client-default':
            params['model'] = model
        started = rpc.call('thread/start', params)
        thread_id = (started.get('thread') or {}).get('id')
        if not isinstance(thread_id, str) or not thread_id or started.get('modelProvider') != PROVIDER:
            raise ClientModelError('Codex did not confirm the expected official provider')
        if started.get('approvalPolicy') != 'never' or (started.get('sandbox') or {}).get('type') != 'readOnly':
            raise ClientModelError('Codex did not confirm restricted chat permissions')
        items = {}
        finished = []
        turn_ids = set()
        event_count = 0
        answer_size = 0

        def event(message):
            nonlocal event_count, answer_size
            event_count += 1
            if event_count > MAX_EVENTS:
                raise ClientModelError('Codex exceeded the chat event limit')
            method = message.get('method', '')
            data = message.get('params') or {}
            if not isinstance(data, dict):
                raise ClientModelError('Codex returned invalid event parameters')
            if data.get('threadId') != thread_id:
                return
            if data.get('turnId'):
                if not isinstance(data['turnId'], str) or len(data['turnId']) > 256:
                    raise ClientModelError('Codex returned an unexpected turn identifier')
                turn_ids.add(data['turnId'])
                if len(turn_ids) > 1:
                    raise ClientModelError('Codex returned an unexpected turn; the response was rejected')
            if method == 'item/completed' or method == 'item/started':
                item = data.get('item') or {}
                if item.get('type') not in {'agentMessage', 'userMessage', 'reasoning', 'contextCompaction'}:
                    raise ClientModelError('Codex reported native tool use; the response was rejected')
                if method == 'item/completed' and item.get('type') == 'agentMessage':
                    text = item.get('text', '')
                    if not isinstance(text, str) or not isinstance(item.get('id'), str) or not 1 <= len(item['id']) <= 256:
                        raise ClientModelError('Codex returned a malformed answer')
                    if item['id'] not in items and len(items) >= MAX_ITEMS:
                        raise ClientModelError('Codex exceeded the chat item limit')
                    answer_size += len(text) - len(items.get(item['id'], {}).get('text', ''))
                    items[item['id']] = item
                    if answer_size > MAX_ANSWER:
                        raise ClientModelError('Codex answer exceeded the chat output limit')
            if method == 'error':
                raise ClientModelError('Codex could not finish the response. Check the account quota and connection; no fallback was used.')
            if method == 'turn/completed':
                if finished:
                    raise ClientModelError('Codex returned an unexpected extra completion')
                finished.append(data.get('turn') or {})

        turn = rpc.call('turn/start', {'threadId': thread_id,
            'input': [{'type': 'text', 'text': prompt}]}, event=event).get('turn') or {}
        turn_id = turn.get('id')
        while not finished:
            event(rpc.receive())
        final = finished[0]
        if not turn_id or final.get('id') != turn_id or turn_ids - {turn_id}:
            raise ClientModelError('Codex returned an unexpected turn; the response was rejected')
        if final.get('status') != 'completed' or final.get('error'):
            raise ClientModelError('Codex did not complete the response. Check quota and retry explicitly.')
        # Some versions include a final summary as well as item notifications.
        # Validate that summary too; it is not an alternate unvalidated answer.
        for item in final.get('items') or []:
            if not isinstance(item, dict) or item.get('type') not in {'agentMessage', 'userMessage', 'reasoning', 'contextCompaction'}:
                raise ClientModelError('Codex reported native tool use; the response was rejected')
        answer = '\n\n'.join(item['text'] for item in items.values()
            if item.get('phase') in (None, 'final_answer')).strip()
        if not answer:
            raise ClientModelError('Codex returned no final text response')
        return answer
    finally:
        rpc.close()


def complete(prompt, model, mode, timeout, cancel=None):
    from src import agent_runners, runner_billing
    if not agent_runners.enabled():
        raise ClientModelError('External agent runners are off. Enable them explicitly in Settings first.')
    if not math.isfinite(float(timeout)) or float(timeout) <= 0:
        raise ClientModelError('Codex chat requires a positive finite timeout')
    if not model or model.startswith('-') or any(c in model for c in '\r\n\0'):
        raise ClientModelError('Invalid Codex model identifier')
    if cancel is not None and cancel.is_set():
        raise ClientModelError('Codex chat cancelled')
    try:
        _, env = runner_billing.prepare('codex', mode, [],
            agent_runners.build_env(agent_runners.get('codex', help_source='')))
        executable = verify_support(env)
        runner_billing.verify('codex', mode, env)
        with tempfile.TemporaryDirectory(prefix='faustus-codex-chat-') as workspace:
            return _infer(executable, env, workspace, prompt, model, mode,
                min(float(timeout), agent_runners.timeout_s()), cancel)
    except ClientModelError:
        raise
    except Exception as exc:
        raise ClientModelError('Codex chat could not start or finish. Check the official client configuration and retry.') from exc
