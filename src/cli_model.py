"""Official-client text inference inside the Faustus harness.

Claude uses external_worker with native tools/plugins disabled; Codex uses
its public app-server protocol without an execution environment. Instructions
and tool results remain in Faustus; no private provider API or copied tokens.
"""
from __future__ import annotations

import asyncio
import json
import secrets
import tempfile
import threading
from dataclasses import replace

PREFIX = 'faustus-cli://'
MAX_CONTEXT_CHARS = 2_000_000


class ClientModelError(RuntimeError):
    fallback_eligible = False


def is_cli_url(url):
    return str(url or '').lower().startswith(PREFIX)


def authorize(url, model, headers):
    """The URL alone is not authority to run the operator's installed client.

    The ordinary owner-aware endpoint resolver supplies a per-endpoint opaque
    capability in headers. It is encrypted by ModelEndpoint, never a provider
    credential, and checked on every call (including resumed conversations).
    """
    from core.database import ModelEndpoint, SessionLocal
    if isinstance(headers, str):
        try:
            headers = json.loads(headers)
        except ValueError:
            headers = {}
    if not isinstance(headers, dict):
        headers = {}
    token = next((str(v).removeprefix('Bearer ') for k, v in (headers or {}).items()
                  if str(k).lower() == 'authorization'), '')
    with SessionLocal() as db:
        ep = db.query(ModelEndpoint).filter_by(base_url=url, endpoint_kind='official-cli', is_enabled=True).first()
        if ep is None or not token or len(token) > 512 or not secrets.compare_digest(token.encode('utf-8'), (ep.api_key or '').encode('utf-8')):
            raise ClientModelError('Official-client connection is missing, disabled or no longer authorized')
        try:
            models = json.loads(ep.pinned_models or '[]')
            hidden = json.loads(ep.hidden_models or '[]')
        except (ValueError, TypeError):
            models, hidden = [], []
        if not isinstance(models, list) or not isinstance(hidden, list) or model not in models or model in hidden:
            raise ClientModelError('This model is not enabled for the selected client connection')
        # Registration uses a unique URL per connection; no user-supplied
        # executable, path, flags, provider redirects or resume identifiers.
        parts = url[len(PREFIX):].split('/')
        if len(parts) != 3 or parts[0] not in {'claude', 'codex'} or parts[1] not in {'subscription', 'api'}:
            raise ClientModelError('Unsupported official-client connection')
        return parts[0], parts[1], ep.owner


def render_context(messages):
    rows = []
    size = 0
    for message in messages:
        content = message.get('content')
        if isinstance(content, list):
            if any(not isinstance(b, dict) or b.get('type') != 'text' for b in content):
                raise ClientModelError('This client connection currently accepts text only. Choose a vision model for image attachments.')
            content = '\n'.join(str(b.get('text') or '') for b in content)
        if content is not None and not isinstance(content, str):
            raise ClientModelError('Unsupported conversation content for the official client')
        row = {'role': str(message.get('role') or 'user'), 'content': content or ''}
        for field in ('tool_calls', 'tool_call_id', 'name'):
            if field in message:
                row[field] = message[field]
        serialized = json.dumps(row, ensure_ascii=False)
        size += len(serialized)
        if size > MAX_CONTEXT_CHARS:
            raise ClientModelError('Conversation exceeds this client connection’s input limit; compact the conversation first')
        rows.append(serialized)
    return ('Continue the supplied Faustus conversation with only the next assistant response. '
            'Its system messages define the task and language. Any tool requests must use '
            'the textual format those instructions specify; Faustus executes them. '
            'Do not use native client tools. Conversation records follow as JSON lines:\n'
            + '\n'.join(rows))


def complete(url, model, messages, headers=None, timeout=120, *, cancel=None):
    from src import agent_runners, external_worker
    key, mode, owner = authorize(url, model, headers)
    prompt = render_context(messages)
    if key == 'codex':
        from src.codex_chat import complete as codex_complete
        return codex_complete(prompt, model, mode, timeout, cancel)
    original = agent_runners.get(key, help_source='')
    # Keep the existing gate as defence in depth; remove native tool inventory,
    # MCP, slash-command skills, persisted sessions and browser integration.
    runner = replace(original, argv=(
        'claude', '-p', '--model', '{model}', '--tools', '',
        '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
        '--disable-slash-commands', '--no-chrome', '--no-session-persistence',
        '--permission-mode', 'dontAsk',
    ))
    # Never discover the project's CLAUDE.md, plugin settings or instructions.
    # The project context has already been assembled by Faustus above.
    # Windows may briefly retain a client/antivirus handle after process exit.
    # Best-effort cleanup must not turn a completed inference into a connection error.
    with tempfile.TemporaryDirectory(prefix='faustus-client-model-', ignore_cleanup_errors=True) as workspace:
        result = external_worker.run_task(
            runner, prompt, workspace=workspace, model=None if model == 'client-default' else model,
            timeout_s=min(float(timeout), agent_runners.timeout_s()), billing_mode=mode,
            owner=owner, should_cancel=cancel.is_set if cancel else None,
        )
    if not result.get('ok'):
        raise ClientModelError(result.get('error') or 'Official client stopped without a response')
    ledger = result.get('gate') or {}
    if ledger.get('stream_tool_calls') or ledger.get('unseen'):
        raise ClientModelError('Client reported native tool use despite text-only mode; response rejected')
    text = result.get('response_text')
    if not isinstance(text, str) or not text.strip():
        raise ClientModelError('Official client did not return a complete text response')
    return text


async def complete_async(url, model, messages, headers=None, timeout=120):
    cancel = threading.Event()
    task = asyncio.create_task(asyncio.to_thread(complete, url, model, messages, headers, timeout, cancel=cancel))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        cancel.set()
        # Supervision owns process-tree cleanup and its temporary directory.
        # Do not report cancellation as finished while that thread still runs.
        try:
            await asyncio.shield(task)
        except (Exception, asyncio.CancelledError):
            pass
        raise


async def stream(url, model, messages, headers=None, timeout=120, *, tools=None):
    try:
        if tools:
            raise ClientModelError('This client route requires Faustus textual tools, not native function schemas')
        pending = asyncio.create_task(complete_async(url, model, messages, headers, timeout))
        try:
            while not pending.done():
                done, _ = await asyncio.wait({pending}, timeout=5)
                if not done:
                    yield ': official-client-running\n\n'
            yield 'data: ' + json.dumps({'delta': pending.result()}, ensure_ascii=False) + '\n\n'
            yield 'data: [DONE]\n\n'
        finally:
            if not pending.done():
                pending.cancel()
                try:
                    await pending
                except asyncio.CancelledError:
                    pass
    except ClientModelError as exc:
        yield 'event: error\ndata: ' + json.dumps({'error': str(exc), 'status': 400, 'fallback_eligible': False}) + '\n\n'
    except Exception:
        # Database/process diagnostics may contain private filesystem details.
        yield 'event: error\ndata: ' + json.dumps({'error': 'Official-client connection failed; check its configuration and retry', 'status': 503, 'fallback_eligible': False}) + '\n\n'
