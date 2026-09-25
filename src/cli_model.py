"""Official-client text inference inside the Faustus harness.

Claude uses external_worker with native tools/plugins disabled; Codex uses
its public app-server protocol without an execution environment. Instructions
and tool results remain in Faustus; no private provider API or copied tokens.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import secrets
import tempfile
import threading
from dataclasses import dataclass, field, replace
from typing import Dict, Tuple

PREFIX = 'faustus-cli://'
MAX_CONTEXT_CHARS = 2_000_000

#: Media types Claude's own image blocks accept (Anthropic's documented list).
#: Anything else is rejected with a clear error rather than sent and silently
#: ignored by the client.
_ALLOWED_IMAGE_TYPES = frozenset({'image/png', 'image/jpeg', 'image/webp', 'image/gif'})
#: Defence in depth against an unbounded attachment set reaching a child
#: process's stdin: a hard cap on how many images and how many total bytes
#: one call to this route will forward.
MAX_IMAGES = 8
MAX_IMAGE_BYTES_TOTAL = 20 * 1024 * 1024

#: Magic-byte signatures for the Ollama-style bare-base64 image shape
#: (a message's own ``images: [<base64>, ...]`` list has no media-type
#: field), so the type is read off the decoded bytes instead of trusted text.
_MAGIC_SNIFFERS: Tuple[Tuple[bytes, str], ...] = (
    (b'\x89PNG\r\n\x1a\n', 'image/png'),
    (b'\xff\xd8\xff', 'image/jpeg'),
    (b'GIF87a', 'image/gif'),
    (b'GIF89a', 'image/gif'),
)


@dataclass(frozen=True)
class RenderedContext:
    """What :func:`render_context` produces: the same textual transcript it
    always produced, plus any images the conversation carried.

    ``images`` is empty for an all-text conversation — the case every caller
    already handles — and non-empty only when the messages themselves
    contained an image block. Each entry is ``{"media_type": "image/png",
    "data": "<base64, no data: prefix>"}``, the shape Claude's own
    ``stream-json`` input format expects for an Anthropic image block.
    """
    prompt: str
    images: Tuple[Dict[str, str], ...] = field(default_factory=tuple)


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


def _decode_base64_image(media_type, data):
    """Validate and decode one base64 image payload, or raise clearly."""
    media_type = str(media_type or '').strip().lower()
    if media_type not in _ALLOWED_IMAGE_TYPES:
        raise ClientModelError(f'Unsupported image type for the official client: {media_type or "unknown"!r}')
    if not data:
        raise ClientModelError('Empty image attachment for the official client')
    try:
        raw = base64.b64decode(str(data), validate=True)
    except (binascii.Error, ValueError):
        raise ClientModelError('Malformed base64 image data for the official client')
    if not raw:
        raise ClientModelError('Empty image attachment for the official client')
    return {'media_type': media_type, 'data': str(data), 'nbytes': len(raw)}


def _decode_data_url_image(url):
    """``data:<media-type>;base64,<data>`` -> a decoded image, or raise.

    A non-``data:`` URL (``http(s)://…``) is refused rather than fetched: this
    client has no network of its own to fetch it with, and silently accepting
    a remote URL here would turn a subscription-authenticated child process
    into an SSRF vector for whoever controls that URL.
    """
    url = str(url or '')
    if not url.startswith('data:'):
        raise ClientModelError('This client connection cannot fetch a remote image URL over the '
                                'network; attach the image as base64 image data instead')
    header, _, data = url.partition(',')
    media_type = header[len('data:'):].split(';')[0].strip().lower()
    return _decode_base64_image(media_type, data)


def _sniff_image_type(raw):
    for signature, media_type in _MAGIC_SNIFFERS:
        if raw.startswith(signature):
            return media_type
    if len(raw) >= 12 and raw[:4] == b'RIFF' and raw[8:12] == b'WEBP':
        return 'image/webp'
    return None


def _decode_bare_base64_image(data):
    """A message's own Ollama-style ``images: [<base64>, …]`` entry.

    This shape carries no media-type field, so the type is read off the
    decoded bytes' own signature rather than guessed or defaulted — an
    unrecognised signature is refused instead of being labelled and sent as
    something it might not be.
    """
    try:
        raw = base64.b64decode(str(data or ''), validate=True)
    except (binascii.Error, ValueError):
        raise ClientModelError('Malformed base64 image data for the official client')
    if not raw:
        raise ClientModelError('Empty image attachment for the official client')
    media_type = _sniff_image_type(raw)
    if media_type is None:
        raise ClientModelError('Could not determine the image type of an attachment for the official client')
    return {'media_type': media_type, 'data': str(data), 'nbytes': len(raw)}


def _extract_image_block(block):
    """One content block -> a decoded image, ``None`` (not an image block, so
    the caller decides what to do with it), or a raised ``ClientModelError``.

    Accepts the OpenAI-style ``image_url`` shape callers in this codebase
    actually build (src/document_processor.py) and the Anthropic-native
    ``image``/``source`` shape, in case a conversation already carries blocks
    in that form (e.g. a message replayed from a prior Claude turn).
    """
    btype = block.get('type')
    if btype == 'image_url':
        return _decode_data_url_image((block.get('image_url') or {}).get('url'))
    if btype == 'image':
        source = block.get('source')
        if not isinstance(source, dict):
            raise ClientModelError('Unsupported image attachment shape for the official client')
        if source.get('type') == 'base64':
            return _decode_base64_image(source.get('media_type'), source.get('data'))
        if source.get('type') == 'url':
            return _decode_data_url_image(source.get('url'))
        raise ClientModelError('Unsupported image attachment shape for the official client')
    return None


def render_context(messages):
    rows = []
    images = []
    total_image_bytes = 0
    size = 0

    def _collect(image):
        nonlocal total_image_bytes
        if len(images) >= MAX_IMAGES:
            raise ClientModelError(f'Too many images for this client connection (max {MAX_IMAGES})')
        total_image_bytes += image['nbytes']
        if total_image_bytes > MAX_IMAGE_BYTES_TOTAL:
            raise ClientModelError('Image attachments exceed this client connection’s size limit '
                                    f'({MAX_IMAGE_BYTES_TOTAL // (1024 * 1024)} MB total)')
        images.append({'media_type': image['media_type'], 'data': image['data']})

    for message in messages:
        content = message.get('content')
        if isinstance(content, list):
            text_parts = []
            for block in content:
                if not isinstance(block, dict):
                    raise ClientModelError('Unsupported conversation content for the official client')
                if block.get('type') == 'text':
                    text_parts.append(str(block.get('text') or ''))
                    continue
                image = _extract_image_block(block)
                if image is None:
                    raise ClientModelError('This client connection accepts text and image content only; '
                                            f'unsupported content block {block.get("type")!r}')
                _collect(image)
            content = '\n'.join(text_parts)
        # Ollama-style bare images on the message itself (no content list).
        raw_images = message.get('images')
        if isinstance(raw_images, list):
            for entry in raw_images:
                _collect(_decode_bare_base64_image(entry))
        if content is not None and not isinstance(content, str):
            raise ClientModelError('Unsupported conversation content for the official client')
        row = {'role': str(message.get('role') or 'user'), 'content': content or ''}
        for field_name in ('tool_calls', 'tool_call_id', 'name'):
            if field_name in message:
                row[field_name] = message[field_name]
        serialized = json.dumps(row, ensure_ascii=False)
        size += len(serialized)
        if size > MAX_CONTEXT_CHARS:
            raise ClientModelError('Conversation exceeds this client connection’s input limit; compact the conversation first')
        rows.append(serialized)
    prompt = ('Continue the supplied Faustus conversation with only the next assistant response. '
              'Its system messages define the task and language. Any tool requests must use '
              'the textual format those instructions specify; Faustus executes them. '
              'Do not use native client tools. Conversation records follow as JSON lines:\n'
              + '\n'.join(rows))
    return RenderedContext(prompt=prompt, images=tuple(images))


def complete(url, model, messages, headers=None, timeout=120, *, cancel=None):
    from src import agent_runners, external_worker
    key, mode, owner = authorize(url, model, headers)
    rendered = render_context(messages)
    if key == 'codex':
        if rendered.images:
            # Codex's non-interactive `exec` protocol has no documented
            # equivalent of Claude's stream-json image blocks; refuse clearly
            # rather than silently dropping the attachment.
            raise ClientModelError('This client connection currently accepts text only. '
                                    'Choose a vision model for image attachments.')
        from src.codex_chat import complete as codex_complete
        return codex_complete(rendered.prompt, model, mode, timeout, cancel)
    original = agent_runners.get(key, help_source='')
    # Keep the existing gate as defence in depth; remove native tool inventory,
    # MCP, slash-command skills, persisted sessions and browser integration.
    argv = (
        'claude', '-p', '--model', '{model}', '--tools', '',
        '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
        '--disable-slash-commands', '--no-chrome', '--no-session-persistence',
        '--permission-mode', 'dontAsk',
    )
    task = rendered.prompt
    if rendered.images:
        # Claude Code's print mode also supports `--input-format stream-json`:
        # JSON lines on stdin instead of plain text, each one an Anthropic
        # message whose content blocks can carry an image. A text-only run
        # (the common case) takes none of this — same argv, same stdin text
        # as before this feature existed.
        argv = argv + ('--input-format', 'stream-json')
        blocks = [{'type': 'image', 'source': {'type': 'base64', 'media_type': img['media_type'],
                                                'data': img['data']}}
                  for img in rendered.images]
        blocks.append({'type': 'text', 'text': rendered.prompt})
        task = json.dumps({'type': 'user', 'message': {'role': 'user', 'content': blocks}},
                           ensure_ascii=False)
    runner = replace(original, argv=argv)
    # Never discover the project's CLAUDE.md, plugin settings or instructions.
    # The project context has already been assembled by Faustus above.
    # Windows may briefly retain a client/antivirus handle after process exit.
    # Best-effort cleanup must not turn a completed inference into a connection error.
    with tempfile.TemporaryDirectory(prefix='faustus-client-model-', ignore_cleanup_errors=True) as workspace:
        result = external_worker.run_task(
            runner, task, workspace=workspace, model=None if model == 'client-default' else model,
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
