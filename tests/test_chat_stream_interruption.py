import asyncio
import copy
import json

import pytest
import routes.chat_routes as cr
from tests.test_lote20_chat_routes_budget_exhausted import _chat_stream_endpoint, _RouteRequest


def test_request_identity_preserves_the_continuation_at_the_end():
    messages = [{'role': 'user', 'content': 'Original request'},
                {'role': 'assistant', 'content': 'Partial answer'},
                {'role': 'user', '_harness_note': True, 'content': 'Continue without repeating.'}]
    assert cr._ensure_current_request_is_latest_user(messages, 'Original request') is messages


@pytest.mark.parametrize("cuts", [1, 4])
@pytest.mark.parametrize("error_class,status", [("transport.connection_reset", 502), ("timeout.inactivity", 504)])
def test_plain_chat_preserves_text_through_transport_cuts(monkeypatch, cuts, error_class, status):
    endpoint = _chat_stream_endpoint(monkeypatch, [])
    monkeypatch.setattr(cr, 'get_session_mode', lambda sid: 'chat')
    requests, saves = [], []
    monkeypatch.setattr(cr, 'save_assistant_response', lambda *args, **kw: saves.append(args[3]) or 'saved')

    async def stream(candidates, messages, **kwargs):
        requests.append(copy.deepcopy(messages))
        yield 'data: ' + json.dumps({'delta': f'Parte {len(requests)}. '}) + '\n\n'
        if len(requests) <= cuts:
            yield 'event: error\ndata: ' + json.dumps({"error": "interrupted", "status": status,
                "error_class": error_class, "partial": True}) + '\n\n'
        else:
            yield 'data: {"type":"finish","finish_reason":"stop"}\n\n'
            yield 'data: [DONE]\n\n'

    monkeypatch.setattr(cr, 'stream_llm_with_fallback', stream)

    async def run():
        request = _RouteRequest()
        request._form['mode'] = 'chat'
        response = await endpoint(request)
        return [chunk async for chunk in response.body_iterator]

    chunks = asyncio.run(run())
    assert len(requests) == cuts + 1
    assert len(saves) == 1
    for n in range(1, cuts + 2):
        assert saves[0].count(f'Parte {n}.') == 1
    assert 'Parte 1.' in str(requests[1])
    assert not any(c.startswith('event: error') for c in chunks)


def test_no_progress_after_chat_recovery_surfaces_the_failure(monkeypatch):
    endpoint = _chat_stream_endpoint(monkeypatch, [])
    monkeypatch.setattr(cr, 'get_session_mode', lambda sid: 'chat')
    requests = []

    async def stream(*args, **kwargs):
        requests.append(True)
        if len(requests) == 1:
            yield 'data: {"delta":"Respuesta parcial. "}\n\n'
        yield 'event: error\ndata: {"error":"reset","status":502,"error_class":"transport.connection_reset","partial":true}\n\n'

    monkeypatch.setattr(cr, 'stream_llm_with_fallback', stream)

    async def run():
        request = _RouteRequest()
        request._form['mode'] = 'chat'
        response = await endpoint(request)
        return [chunk async for chunk in response.body_iterator]

    chunks = asyncio.run(run())
    assert len(requests) == 2
    assert any(c.startswith('event: error') for c in chunks)
