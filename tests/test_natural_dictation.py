import asyncio
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.stt.natural_dictation import faithful_deletions, polish
from routes.stt_routes import setup_stt_routes


def resolve(*args, **kwargs):
    return "http://127.0.0.1:8081/v1/chat/completions", "test-main", {}


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["Sí.", "No", "¡Gracias!", "OK", "42", "Thank you."])
async def test_short_answers_need_no_model(text):
    def unavailable(*args, **kwargs):
        raise AssertionError("Short answer must not resolve or call a model")
    result = await polish(text, resolve=unavailable)
    assert result == {"text": text, "raw_text": text, "status": "unchanged"}


@pytest.mark.parametrize("raw,edited,accepted", [
    ("Eh compra tres perdón dos litros", "Compra dos litros.", True),
    ("No, no, no quiero borrar nada", "No, no, no quiero borrar nada.", True),
    ("It gets everybody doesn't have cabin fever", "It gets everybody out so nobody has cabin fever", False),
    ("El precio es 1,50", "El precio es 150", False),
    ("Quiero borrar notas", "", False),
    ("umm eeeh", "", True),
    ("No borres nada", "Nada borres no", False),
    ('La frase era «eh, no, espera»', 'La frase era «no, espera»', False),
    ('Eh di "umm espera"', 'Di "umm espera".', True),
    ('Di “eh espera”', 'Di “espera”', False),
])
def test_cleanup_cannot_add_or_reorder_words(raw, edited, accepted):
    assert faithful_deletions(raw, edited) == accepted


@pytest.mark.asyncio
async def test_editor_returns_original_and_has_no_tools_or_chat_history():
    calls = []
    async def complete(**kwargs):
        calls.append(kwargs)
        return json.dumps({"text": "Compra dos litros."})
    result = await polish("Eh compra tres perdón dos litros", complete=complete, resolve=resolve)
    assert result["text"] == "Compra dos litros."
    assert result["raw_text"] == "Eh compra tres perdón dos litros"
    assert result["status"] == "edited"
    assert calls[0]["gen_overrides"] == {"think": False}
    assert len(calls[0]["messages"]) == 2
    assert "tools" not in calls[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("response", ['{"text":"A new invented claim."}', '{"text":null}', 'broken JSON', '{"text":""}'])
async def test_failed_edit_preserves_user_words(response):
    async def complete(**kwargs):
        return response
    result = await polish("No borres las notas", complete=complete, resolve=resolve)
    assert result["text"] == "No borres las notas"
    assert result["status"] == "fallback"


@pytest.mark.asyncio
async def test_timeout_preserves_draft_and_cancel_propagates():
    async def timeout(**kwargs):
        raise TimeoutError()
    result = await polish("Cambia martes por jueves", "revise", "El martes.", complete=timeout, resolve=resolve)
    assert result["text"] == "El martes."
    assert result["status"] == "fallback"
    async def cancel(**kwargs):
        raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await polish("Hola", complete=cancel, resolve=resolve)


@pytest.mark.asyncio
async def test_explicit_revision_can_replace_draft_words():
    async def complete(**kwargs):
        payload = json.loads(kwargs["messages"][1]["content"])
        assert payload["draft"] == "El martes."
        assert payload["mode"] == "REVISE"
        return '{"text":"El jueves."}'
    result = await polish("Cambia martes por jueves", "revise", "El martes.", complete=complete, resolve=resolve)
    assert result["text"] == "El jueves."


def test_route_contract_and_validation(monkeypatch):
    import services.stt.natural_dictation as editor
    async def fake(text, mode, draft, **kwargs):
        return {"text": text, "raw_text": text, "status": "unchanged"}
    monkeypatch.setattr(editor, "polish", fake)
    app = FastAPI()
    app.include_router(setup_stt_routes(None))
    with TestClient(app) as client:
        assert client.post("/api/stt/polish", json={"text": "Hola"}).json()["text"] == "Hola"
        assert client.post("/api/stt/polish", json={"text": "x" * 8001}).status_code == 422
        assert client.post("/api/stt/polish", json={"text": "Cambia", "mode": "revise"}).status_code == 422
        assert client.post("/api/stt/polish", json={"text": "Cambia", "mode": "execute"}).status_code == 422
