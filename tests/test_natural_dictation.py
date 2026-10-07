import asyncio
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.stt.natural_dictation import faithful_deletions, polish
from routes.stt_routes import setup_stt_routes


def resolve(*args, **kwargs):
    return "http://127.0.0.1:8081/v1/chat/completions", "test-main", {}


def test_formatting_evaluation_checks_line_and_paragraph_boundaries():
    from scripts.eval_natural_dictation import matches_expected
    assert not matches_expected("Hola Ana. Nos vemos mañana.", ["Hola Ana.\n\nNos vemos mañana."])
    assert not matches_expected("Hola Ana.\nNos vemos mañana.", ["Hola Ana.\n\nNos vemos mañana."])
    assert matches_expected("Hola, Ana.\n\nNos vemos mañana.", ["Hola Ana.\n\nNos vemos mañana."])


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
    ("La temperatura es -12 grados", "La temperatura es 12 grados", False),
    ("Aplica el 15%", "Aplica el 15", False),
    ("Necesito 3/4 de litro", "Necesito 3 de litro", False),
    ("Nos vemos a las 12:30", "Nos vemos a las 12", False),
    ("Eh son − 12 grados y 15 %", "Son -12 grados y 15%.", True),
    ("Pon -12, perdón, -10 grados", "Pon -10 grados.", True),
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
@pytest.mark.parametrize("lines,expected", [
    (["Ingredients", "milk", "eggs"], "Ingredients\nmilk\neggs"),
    (["Ingredients", "", "milk", "eggs"], "Ingredients\n\nmilk\neggs"),
])
async def test_line_arrays_preserve_words_and_paragraph_breaks(lines, expected):
    async def complete(**kwargs):
        return json.dumps({"text": lines})
    result = await polish("Ingredients new line milk new line eggs", complete=complete, resolve=resolve)
    assert result["text"] == expected
    assert result["status"] == "edited"


@pytest.mark.asyncio
@pytest.mark.parametrize("lines", [["Ingredients", None], [["milk"]], ["Ingredients", "e"], [""] * 8001])
async def test_invalid_line_arrays_keep_original(lines):
    async def complete(**kwargs):
        return json.dumps({"text": lines})
    source = "Ingredients new line milk new line eggs"
    result = await polish(source, complete=complete, resolve=resolve)
    assert result["text"] == source
    assert result["status"] == "fallback"


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
    assert calls[0]["timeout"] == 12


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["clean", "revise"])
async def test_long_dictation_and_long_draft_get_time_to_return_complete_text(mode):
    draft = "La propuesta requiere revisar las cifras y mantener las notas anteriores. " * 15
    instruction = draft if mode == "clean" else "Conserva el texto tal cual."
    calls = []
    async def complete(**kwargs):
        calls.append(kwargs)
        return json.dumps({"text": draft})
    result = await polish(instruction, mode, draft if mode == "revise" else "", complete=complete, resolve=resolve)
    assert result["text"] == draft.strip()
    assert result["status"] != "fallback"
    assert 12 < calls[0]["timeout"] <= 90


@pytest.mark.asyncio
@pytest.mark.parametrize("response", ['{"text":"A new invented claim."}', '{"text":null}', 'broken JSON', '{"text":""}'])
async def test_failed_edit_preserves_user_words(response):
    async def complete(**kwargs):
        return response
    result = await polish("No borres las notas", complete=complete, resolve=resolve)
    assert result["text"] == "No borres las notas"
    assert result["status"] == "fallback"


@pytest.mark.asyncio
@pytest.mark.parametrize("response", ['{"textVC', '{}', 'null', '{"text":[null]}'])
async def test_malformed_model_output_is_distinct_from_transport_failure(response):
    async def complete(**kwargs):
        return response
    draft = "Hola Ana. Nos vemos el martes."
    result = await polish("Cambia martes por jueves", "revise", draft, complete=complete, resolve=resolve)
    assert result["text"] == draft
    assert result["reason"] == "invalid_model_output"
    assert result["status"] == "fallback"


@pytest.mark.asyncio
async def test_timeout_preserves_draft_and_cancel_propagates():
    async def timeout(**kwargs):
        raise TimeoutError()
    result = await polish("Cambia martes por jueves", "revise", "El martes.", complete=timeout, resolve=resolve)
    assert result["text"] == "El martes."
    assert result["status"] == "fallback"
    assert result["reason"] == "editor_unavailable"
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
