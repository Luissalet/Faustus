"""A transport EOF is not proof that a model completed its response."""
import json

import pytest

from tests.test_llm_core_streaming_retries import _drive, _events, _Resp, _StreamCtx


@pytest.mark.parametrize("delta", [
    {"content": "La respuesta sigue"},
    {"reasoning_content": "Pensando"},
    {"tool_calls": [{"index": 0, "id": "c1", "type": "function",
                     "function": {"name": "bash", "arguments": '{"command":"'}}]},
])
def test_eof_without_provider_completion_is_incomplete(monkeypatch, delta):
    client, chunks = _drive(monkeypatch, [_StreamCtx(resp=_Resp(lines=[
        'data: ' + json.dumps({"choices": [{"delta": delta}]}),
    ]))])
    events = _events(chunks)
    assert client.calls == 1
    assert any(e.get("error_class") == "transport.incomplete_stream" for e in events)
    assert not any(e.get("type") == "tool_calls" for e in events)
    assert not any("data: [DONE]" in c for c in chunks)


def test_eof_after_finish_reason_is_complete(monkeypatch):
    client, chunks = _drive(monkeypatch, [_StreamCtx(resp=_Resp(lines=[
        'data: {"choices":[{"delta":{"content":"Respuesta completa"}}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
    ]))])
    assert client.calls == 1
    assert not any("error" in e for e in _events(chunks))
    assert any("data: [DONE]" in c for c in chunks)
