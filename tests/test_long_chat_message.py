"""Long chat text is governed by context, not a transport character limit."""
import hashlib
import io

import pytest
from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.testclient import TestClient
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from src.chat_helpers import validate_message, validate_file_upload
from src.first_token_budget import effective, request_budget
from src.request_models import ChatRequest

# More than both the former50K cap and Starlette's default1MiB text part.
LONG = ('á漢🙂\n' * 300_000) + 'END'


@pytest.mark.parametrize('message', [LONG, 'á漢🙂' * 20_000], ids=['over_1MiB', 'over_50K'])
def test_long_text_validators_preserve_every_character(message):
    assert validate_message(message) == message
    assert ChatRequest(message=message, session='own').message == message


@pytest.mark.parametrize('message', ['', ' \n\t '])
def test_empty_message_remains_invalid(message):
    with pytest.raises(HTTPException):
        validate_message(message)


def test_request_model_still_requires_nonempty_string():
    assert ChatRequest.model_json_schema()['properties']['message']['minLength'] == 1
    assert 'maxLength' not in ChatRequest.model_json_schema()['properties']['message']
    with pytest.raises(ValidationError):
        ChatRequest(message='', session='own')
    with pytest.raises(ValidationError):
        ChatRequest(message=None, session='own')


@pytest.fixture
def client():
    app = FastAPI()
    @app.post('/turn')
    @request_budget
    async def turn(request: Request):
        form = await request.form()
        message = validate_message(form['message'])
        file = form.get('file')
        file_bytes = await file.read() if file else None
        return JSONResponse({'sha256': hashlib.sha256(message.encode()).hexdigest(),
                'chars': len(message), 'budget': effective(None),
                'file': file_bytes.decode() if file_bytes is not None else None})
    with TestClient(app) as connection:
        yield connection


@pytest.mark.parametrize('with_file', [False, True])
def test_multipart_text_over_default_part_limit_and_file_stay_intact(client, with_file):
    fields = [('message', (None, LONG)), ('initial_response_timeout_s', (None, '900'))]
    if with_file:
        fields.append(('file', ('note.txt', b'original attachment', 'text/plain')))
    response = client.post('/turn', files=fields)
    assert response.status_code == 200, response.text
    assert response.json() == {'sha256': hashlib.sha256(LONG.encode()).hexdigest(),
        'chars': len(LONG), 'budget': 900,
        'file': 'original attachment' if with_file else None}


@pytest.mark.parametrize('value', ['4', '901', 'NaN', 'inf', 'bad'])
def test_large_form_keeps_turn_timeout_validation(client, value):
    response = client.post('/turn', files={'message': (None, LONG),
                                          'initial_response_timeout_s': (None, value)})
    assert response.status_code == 422
    assert 'initial_response_timeout_s' in response.text


def test_file_validation_is_not_disabled(monkeypatch):
    import src.chat_helpers as helpers
    monkeypatch.setattr(helpers, 'get_chat_upload_max_bytes', lambda: 4)
    with pytest.raises(HTTPException) as exc:
        validate_file_upload(UploadFile(filename='large.txt', file=io.BytesIO(b'12345')))
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException):
        validate_file_upload(UploadFile(filename='empty.txt', file=io.BytesIO(b'')))


@pytest.mark.parametrize('encoding', ['json', 'multipart', 'multipart_file'])
def test_native_stream_route_delivers_full_text_to_context_builder(monkeypatch, encoding):
    # Use the actual registered route and FastAPI dependency machinery; only
    # the downstream context/provider are fixtures, not the request parser.
    from tests.test_foreground_model_routing import _chat_stream_endpoint
    from src.chat_helpers import coerce_message_and_session
    import routes.chat_routes as chat_routes
    captured = {}
    endpoint = _chat_stream_endpoint(monkeypatch, 'chat', captured, capture_context=True)
    monkeypatch.setattr(chat_routes, 'coerce_message_and_session', coerce_message_and_session)
    app = FastAPI()
    app.post('/api/chat_stream')(endpoint)
    body = {'session': 'session-1', 'message': LONG, 'mode': 'chat',
            'compare_mode': 'true', 'initial_response_timeout_s': 900}
    with TestClient(app) as client:
        if encoding == 'json':
            response = client.post('/api/chat_stream', json=body)
        else:
            parts = [(key, (None, str(value))) for key, value in body.items()]
            if encoding == 'multipart_file':
                parts.append(('files', ('note.txt', b'attachment bytes', 'text/plain')))
            response = client.post('/api/chat_stream', files=parts)
    assert response.status_code == 200, response.text
    assert '[DONE]' in response.text
    actual = captured['build_context']['message']
    assert len(actual) == len(LONG)
    assert hashlib.sha256(actual.encode()).digest() == hashlib.sha256(LONG.encode()).digest()
    assert response.headers['X-Faustus-Initial-Response-Timeout-S'] == '900.0'


def test_form_field_count_keeps_the_framework_default(client):
    parts = [('message', (None, 'hello'))]
    parts.extend((f'extra{index}', (None, 'value')) for index in range(1000))
    response = client.post('/turn', files=parts)
    assert response.status_code == 400
    assert 'Too many fields' in response.text

