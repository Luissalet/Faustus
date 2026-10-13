"""Attachment MCP operations use the ordinary native HTTP contracts."""
import asyncio
import json
from email.parser import BytesParser
from email.policy import default

import httpx
import pytest
from mcp_servers import hoard_copilot_server as copilot


def wire(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setenv('FAUSTUS_URL', 'http://private-copilot.test')
    monkeypatch.delenv('FAUSTUS_TOKEN_FILE', raising=False)
    monkeypatch.setattr(copilot.httpx, 'AsyncClient', lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(handler)))


def parts(request):
    message = BytesParser(policy=default).parsebytes(
        b'Content-Type: ' + request.headers['content-type'].encode() + b'\r\n\r\n' + request.content)
    return {part.get_param('name', header='content-disposition'): part for part in message.iter_parts()}


def test_mcp_upload_forwards_file_bytes_and_session(monkeypatch, tmp_path):
    path = tmp_path / 'informe.csv'
    path.write_bytes('año,importe\n2026,7\n'.encode())
    received = []
    def handler(request):
        received.append(request)
        form = parts(request)
        assert request.url.path == '/api/upload'
        assert form['files'].get_filename() == path.name
        assert form['files'].get_payload(decode=True) == path.read_bytes()
        assert form['session_id'].get_payload(decode=True) == b'qa-session'
        return httpx.Response(200, json={'files': [{'id': 'native-id', 'name': path.name}]})
    wire(monkeypatch, handler)
    result = asyncio.run(copilot.call_tool('hoard_attachment_upload', {'paths': [str(path)], 'session_id': 'qa-session'}))
    assert json.loads(result[0].text)['files'][0]['id'] == 'native-id'
    assert len(received) == 1


def test_mcp_turn_attachment_ids_use_multipart_not_ignored_json(monkeypatch):
    def handler(request):
        form = parts(request)
        assert request.url.path == '/api/chat_stream'
        assert form['session'].get_payload(decode=True) == b'qa-session'
        assert json.loads(form['attachments'].get_payload(decode=True)) == ['native-id']
        assert json.loads(form['hoard_context'].get_payload(decode=True))['origin']['app_id'] == 'writer'
        assert set(form) == {'session', 'message', 'hoard_context', 'mode', 'attachments'}
        return httpx.Response(200, text='data: [DONE]\n\n', headers={'X-Odysseus-Run-Id': 'native-run'})
    wire(monkeypatch, handler)
    result = asyncio.run(copilot.call_tool('hoard_turn_start', {'session_id': 'qa-session', 'message': 'Read file',
        'context': {'origin': {'app_id': 'writer', 'data_space_id': 'private'}}, 'attachments': ['native-id']}))
    assert json.loads(result[0].text)['run_id'] == 'native-run'


@pytest.mark.parametrize('ids', [None, [], ['native-id']])
def test_draft_patch_preserves_omitted_ids_and_forwards_explicit_lists(monkeypatch, ids):
    def handler(request):
        assert request.method == 'PATCH' and request.url.path == '/api/sessions/qa-session/draft'
        body = json.loads(request.content)
        assert body['text'] == 'Pending message'
        assert ('attachment_ids' in body) == (ids is not None)
        if ids is not None:
            assert body['attachment_ids'] == ids
        return httpx.Response(200, json=body)
    wire(monkeypatch, handler)
    payload = {'session_id': 'qa-session', 'text': 'Pending message'}
    if ids is not None:
        payload['attachment_ids'] = ids
    asyncio.run(copilot.call_tool('hoard_draft_save', payload))


def test_mcp_catalog_exposes_upload_and_draft_capabilities():
    tools = {tool.name: tool for tool in asyncio.run(copilot.list_tools())}
    assert {'hoard_attachment_upload', 'hoard_draft_get', 'hoard_draft_save'} <= tools.keys()
    assert 'attachments' in tools['hoard_turn_start'].inputSchema['properties']


@pytest.mark.parametrize('method,body,text,ids', [
    ('PATCH', {'attachment_ids': ['two']}, 'Original message', ['two']),
    ('PATCH', {'text': 'Changed'}, 'Changed', ['one', 'two']),
    ('PATCH', {}, 'Original message', ['one', 'two']),
    ('PUT', {'attachment_ids': ['two']}, '', ['two']),
    ('PUT', {'text': 'Changed'}, 'Changed', []),
    ('PUT', {}, '', []),
])
def test_native_draft_patch_preserves_omitted_fields_and_put_replaces(tmp_path, monkeypatch, method, body, text, ids):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes import session_routes
    from src import session_draft
    monkeypatch.setattr(session_draft, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(session_routes, '_verify_session_owner', lambda *args: None)
    monkeypatch.setattr(session_routes, 'effective_user', lambda request: 'alice')
    session_draft.save('qa', 'alice', 'Original message', ['one', 'two'])
    app = FastAPI()
    app.include_router(session_routes.setup_session_routes(object(), {}))
    with TestClient(app) as client:
        response = client.request(method, '/api/sessions/qa/draft', json=body)
    assert response.status_code == 200, response.text
    assert response.json()['text'] == text
    assert response.json()['attachment_ids'] == ids
