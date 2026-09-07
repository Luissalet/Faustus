"""Real installed client + loopback Responses fixture. No account or paid call."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import sys
import threading

import pytest

from src import codex_chat as chat


@pytest.mark.skipif(not shutil.which('codex'), reason='Official Codex binary not installed')
def test_real_client_uses_environmentless_thread_and_no_inherited_mcp(monkeypatch, tmp_path):
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            length = int(self.headers.get('Content-Length', '0'))
            assert length < 2_000_000
            request = json.loads(self.rfile.read(length))
            requests.append(request)
            item = {'id': 'message_fixture', 'type': 'message', 'status': 'completed', 'role': 'assistant',
                    'content': [{'type': 'output_text', 'text': 'Hola ✓', 'annotations': []}]}
            response = {'id': 'response_fixture', 'object': 'response', 'created_at': 1, 'status': 'completed',
                        'output': [item], 'model': 'gpt-5.5', 'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}}
            events = [
                {'type': 'response.created', 'response': {**response, 'status': 'in_progress', 'output': []}},
                {'type': 'response.output_item.added', 'output_index': 0, 'item': {**item, 'status': 'in_progress', 'content': []}},
                {'type': 'response.output_text.delta', 'item_id': item['id'], 'output_index': 0, 'content_index': 0, 'delta': 'Hola ✓'},
                {'type': 'response.output_item.done', 'output_index': 0, 'item': item},
                {'type': 'response.completed', 'response': response},
            ]
            payload = ''.join('event: ' + e['type'] + '\ndata: ' + json.dumps(e) + '\n\n' for e in events).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client_home = tmp_path / 'client-home'; client_home.mkdir()
    workspace = tmp_path / 'workspace'; workspace.mkdir()
    # A hostile inherited command must not be started, even with a dotted key.
    marker = tmp_path / 'mcp-was-started.txt'
    canary = tmp_path / 'inert_mcp_canary.py'
    canary.write_text('from pathlib import Path\nPath(' + repr(str(marker)) + ').write_text("started")\n', encoding='utf-8')
    (client_home / 'config.toml').write_text(
        '[mcp_servers."inert.canary"]\ncommand=' + json.dumps(sys.executable)
        + '\nargs=[' + json.dumps(str(canary)) + ']\n', encoding='utf-8')
    env = {k: v for k, v in os.environ.items()
           if not any(part in k.upper() for part in ('TOKEN', 'API_KEY', 'SECRET'))}
    env['CODEX_HOME'] = str(client_home)
    env.pop('OPENAI_BASE_URL', None)
    monkeypatch.setattr(chat, 'PROVIDER', 'fixture')
    original_command = chat._command
    def command(*args):
        result = original_command(*args)
        at = result.index('forced_login_method="chatgpt"')
        del result[at - 1:at + 1]  # Synthetic provider has no authentication.
        return [*result, '-c', 'model_providers.fixture.name="Inert local fixture"',
            '-c', f'model_providers.fixture.base_url="http://127.0.0.1:{server.server_port}/v1"',
            '-c', 'model_providers.fixture.wire_api="responses"',
            '-c', 'model_providers.fixture.requires_openai_auth=false', '--disable', 'enable_request_compression']
    monkeypatch.setattr(chat, '_command', command)
    original_call = chat._Rpc.call
    def call(self, method, params, *args, **kwargs):
        if method == 'account/read':
            return {'account': {'type': 'chatgpt'}}  # No user credentials read/copied.
        return original_call(self, method, params, *args, **kwargs)
    monkeypatch.setattr(chat._Rpc, 'call', call)
    original_receive = chat._Rpc.receive
    def receive(self):
        message = original_receive(self)
        assert 'error' not in message, message.get('error')
        return message
    monkeypatch.setattr(chat._Rpc, 'receive', receive)
    try:
        executable = chat.verify_support(env)
        answer = chat._infer(executable, env, str(workspace), 'Reply Hola.', 'gpt-5.5', 'subscription', 20, None)
        assert answer == 'Hola ✓'
        assert requests
        tools = {tool.get('name', tool.get('type')) for request in requests for tool in request.get('tools', [])}
        assert tools <= {'request_user_input', 'skills'}
        assert not marker.exists(), 'Inherited MCP process must never start'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
