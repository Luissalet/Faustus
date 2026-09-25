"""Vision support for the official Claude client route (src/cli_model.py).

Claude Code's print mode accepts `--input-format stream-json` (JSON lines on
stdin, each an Anthropic message whose content blocks can carry an image) in
addition to its default plain-text stdin. These tests pin:

* the text-only path is byte-identical to what it was before this feature —
  same argv, same stdin string;
* the image path builds the right stream-json line and adds the flag;
* a remote (non-`data:`) image URL, too many images, and an oversized total
  are all refused with a clear error rather than silently accepted or sent;
* Codex — which has no documented equivalent protocol — still refuses images
  with the original, unchanged error;
* the base64 image payload never reaches a log record.
"""
import base64
import json
import logging

import pytest

from src import cli_model as client, external_worker as worker

# A real, tiny (1x1, 67-byte) PNG — its own magic bytes matter for the
# Ollama-style bare-base64 path, which sniffs the type instead of trusting a
# caller-supplied label.
_PNG_B64 = ('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk'
            '+A8AAQUBAScY42YAAAAASUVORK5CYII=')
_PNG_BYTES = base64.b64decode(_PNG_B64)


def _image_url_message(b64=_PNG_B64, media_type='image/png'):
    return {'role': 'user', 'content': [
        {'type': 'text', 'text': 'What is in this picture?'},
        {'type': 'image_url', 'image_url': {'url': f'data:{media_type};base64,{b64}'}},
    ]}


@pytest.fixture
def authorized_claude(monkeypatch):
    monkeypatch.setattr(client, 'authorize', lambda *a: ('claude', 'subscription', 'alice'))


@pytest.fixture
def authorized_codex(monkeypatch):
    monkeypatch.setattr(client, 'authorize', lambda *a: ('codex', 'subscription', 'alice'))


# ── render_context: the shared image parsing ────────────────────────────────

def test_render_context_accepts_a_data_url_image():
    rendered = client.render_context([_image_url_message()])
    assert len(rendered.images) == 1
    assert rendered.images[0] == {'media_type': 'image/png', 'data': _PNG_B64}
    assert 'What is in this picture?' in rendered.prompt


def test_render_context_accepts_anthropic_native_image_blocks():
    rendered = client.render_context([{'role': 'user', 'content': [
        {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/jpeg', 'data': _PNG_B64}},
    ]}])
    assert rendered.images == ({'media_type': 'image/jpeg', 'data': _PNG_B64},)


def test_render_context_sniffs_ollama_style_bare_images():
    rendered = client.render_context([{'role': 'user', 'content': 'Describe it', 'images': [_PNG_B64]}])
    assert rendered.images == ({'media_type': 'image/png', 'data': _PNG_B64},)


def test_remote_data_url_is_rejected():
    with pytest.raises(client.ClientModelError, match='remote image URL'):
        client.render_context([{'role': 'user', 'content': [
            {'type': 'image_url', 'image_url': {'url': 'https://example.test/cat.png'}}]}])


def test_anthropic_url_source_is_also_rejected():
    with pytest.raises(client.ClientModelError, match='remote image URL'):
        client.render_context([{'role': 'user', 'content': [
            {'type': 'image', 'source': {'type': 'url', 'url': 'https://example.test/cat.png'}}]}])


def test_unsupported_media_type_is_rejected():
    with pytest.raises(client.ClientModelError, match='Unsupported image type'):
        client.render_context([_image_url_message(media_type='image/bmp')])


def test_malformed_base64_is_rejected():
    with pytest.raises(client.ClientModelError, match='Malformed base64'):
        client.render_context([_image_url_message(b64='not-base64-!!!')])


def test_unsniffable_bare_image_is_rejected():
    junk = base64.b64encode(b'not a real image').decode()
    with pytest.raises(client.ClientModelError, match='Could not determine the image type'):
        client.render_context([{'role': 'user', 'content': 'x', 'images': [junk]}])


def test_too_many_images_is_rejected():
    messages = [_image_url_message() for _ in range(client.MAX_IMAGES + 1)]
    with pytest.raises(client.ClientModelError, match='Too many images'):
        client.render_context(messages)


def test_oversized_total_is_rejected(monkeypatch):
    monkeypatch.setattr(client, 'MAX_IMAGE_BYTES_TOTAL', len(_PNG_BYTES))
    with pytest.raises(client.ClientModelError, match='size limit'):
        client.render_context([_image_url_message(), _image_url_message()])


def test_other_unsupported_content_block_still_rejected():
    with pytest.raises(client.ClientModelError, match='text and image content only'):
        client.render_context([{'role': 'user', 'content': [{'type': 'audio', 'audio': {'url': 'data:...'}}]}])


# ── complete(): text-only path is unchanged ─────────────────────────────────

def test_text_only_path_is_byte_identical_to_before(authorized_claude, monkeypatch):
    captured = {}

    def run(runner, task, **kwargs):
        captured['argv'] = runner.argv
        captured['task'] = task
        return {'ok': True, 'response_text': 'Hi back'}

    monkeypatch.setattr(worker, 'run_task', run)
    result = client.complete('url', 'client-default', [{'role': 'user', 'content': 'Hi'}])
    assert result == 'Hi back'
    assert '--input-format' not in captured['argv']
    assert captured['argv'] == (
        'claude', '-p', '--model', '{model}', '--tools', '',
        '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
        '--disable-slash-commands', '--no-chrome', '--no-session-persistence',
        '--permission-mode', 'dontAsk',
    )
    # The stdin text is the plain rendered transcript, not a JSON envelope.
    assert captured['task'] == client.render_context([{'role': 'user', 'content': 'Hi'}]).prompt


# ── complete(): the image path ──────────────────────────────────────────────

def test_image_path_adds_input_format_and_stream_json_line(authorized_claude, monkeypatch):
    captured = {}

    def run(runner, task, **kwargs):
        captured['argv'] = runner.argv
        captured['task'] = task
        return {'ok': True, 'response_text': 'A cat'}

    monkeypatch.setattr(worker, 'run_task', run)
    result = client.complete('url', 'client-default', [_image_url_message()])
    assert result == 'A cat'

    argv = captured['argv']
    assert argv[-2:] == ('--input-format', 'stream-json')
    # Text-only flags are all still present, unchanged.
    assert argv[:13] == (
        'claude', '-p', '--model', '{model}', '--tools', '',
        '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
        '--disable-slash-commands', '--no-chrome', '--no-session-persistence',
        '--permission-mode',
    )

    line = json.loads(captured['task'])
    assert line['type'] == 'user'
    content = line['message']['content']
    assert line['message']['role'] == 'user'
    assert content[0] == {'type': 'image', 'source': {
        'type': 'base64', 'media_type': 'image/png', 'data': _PNG_B64}}
    assert content[-1]['type'] == 'text'
    assert 'What is in this picture?' in content[-1]['text']
    # Exactly one JSON line: no embedded raw newline broke it in two.
    assert '\n' not in captured['task']


def test_codex_still_rejects_images_with_the_original_message(authorized_codex, monkeypatch):
    monkeypatch.setattr(worker, 'run_task', lambda *a, **k: pytest.fail('codex must not be spawned'))
    with pytest.raises(client.ClientModelError, match='text only'):
        client.complete('url', 'model', [_image_url_message()])


# ── the payload never reaches a log record ──────────────────────────────────

def test_base64_payload_never_appears_in_log_records(authorized_claude, monkeypatch, caplog):
    def run(runner, task, **kwargs):
        return {'ok': True, 'response_text': 'A cat'}

    monkeypatch.setattr(worker, 'run_task', run)
    with caplog.at_level(logging.DEBUG):
        client.complete('url', 'client-default', [_image_url_message()])
    for record in caplog.records:
        assert _PNG_B64 not in record.getMessage()
