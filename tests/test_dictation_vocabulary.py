import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src import dictation_vocabulary as store


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    from src import constants
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))


def test_spelling_scope_boundaries_literals_and_no_cascades():
    store.put_vocabulary("fausto", "Faustus", owner="luis")
    store.put_vocabulary("Faustus", "Faustus Core", owner="luis")
    store.put_vocabulary("fausto", "Fausto Project", owner="luis", project="p1")
    assert store.apply_vocabulary('fausto -12 15% no borres "fausto" C:/fausto/a.py fausto.py faustoso', "luis") == 'Faustus -12 15% no borres "fausto" C:/fausto/a.py fausto.py faustoso'
    assert store.apply_vocabulary("fausto", "luis", "p1") == "Fausto Project"
    assert store.apply_vocabulary("fausto", "other", "p1") == "fausto"
    assert store.apply_vocabulary("fausto", "luis", "p2") == "Faustus"


@pytest.mark.parametrize("a,b", [("doce", "12"), ("-12", "12"), ("no", "sí"), ("foo.py/a", "Bar"), ("a", "")])
def test_vocabulary_is_not_a_numeric_or_negation_rewrite(a, b):
    with pytest.raises(ValueError):
        store.put_vocabulary(a, b)


def test_correction_learns_name_but_keeps_original_and_owner():
    entry = store.remember("Visita fausto -12", "Visita fausto -12", owner="luis", project="p1", status="paste_failed")
    result = store.correct(entry, "Visita Faustus -12", owner="luis")
    assert result["learned"][0]["replacement"] == "Faustus"
    assert store.history("luis", "p1")[0]["raw_text"] == "Visita fausto -12"
    assert store.history("other", "p1") == []
    assert not store.delete_history(entry, "other")
    with pytest.raises(KeyError):
        store.correct(entry, "Secret", owner="other")
    assert store.apply_vocabulary("fausto", "luis", "p1") == "Faustus"
    assert store.apply_vocabulary("fausto", "luis") == "fausto"
    assert store.delete_vocabulary(result["learned"][0]["id"], "luis")
    assert store.apply_vocabulary("fausto", "luis", "p1") == "fausto"


@pytest.mark.parametrize("a,b", [("No borres 12 notas", "Borra 13 notas"), ("mañana voy a pasear", "hoy me quedo descansando"), ("un gato", "el perro")])
def test_rewrites_numbers_and_negations_are_not_learned(a, b):
    assert store.learn_corrections(a, b) == []


@pytest.mark.asyncio
async def test_editor_fidelity_check_precedes_explicit_vocabulary():
    from services.stt.natural_dictation import polish
    store.put_vocabulary("fausto", "Faustus", owner="luis")
    async def complete(**kw):
        return json.dumps({"text": "Fausto cuesta 12"})  # invention: wrong sign
    result = await polish("fausto cuesta -12", owner="luis", complete=complete,
                          resolve=lambda *a, **k: ("http://127.0.0.1:1/v1", "fixture", {}))
    assert result["text"] == "Faustus cuesta -12"
    assert result["reason"] == "non_faithful_edit"


def api(monkeypatch, text="Eh visita fausto"):
    import routes.dictation_routes as routes
    from src import dictation_paste as dp
    monkeypatch.setattr(dp, "IS_WINDOWS", True)
    monkeypatch.setattr(routes, "owner_is_admin_or_single_user", lambda x: True)
    app = FastAPI()
    @app.middleware("http")
    async def identity(request: Request, call_next):
        request.state.current_user = "luis"
        return await call_next(request)
    app.include_router(routes.setup_dictation_routes(SimpleNamespace(available=True, transcribe=lambda *a, **k: text)))
    return TestClient(app, base_url="http://testclient")


def test_global_pipeline_polishes_saves_before_paste_and_recovers_on_os_error(monkeypatch):
    from services.stt import natural_dictation
    from src import dictation_paste as dp
    calls = []
    async def polish(text, **kw):
        calls.append((text, kw))
        return {"text": "Visita Faustus", "raw_text": text, "status": "edited"}
    def paste(text, **kw):
        assert store.history("luis")[0]["text"] == text
        raise OSError("Target closed")
    monkeypatch.setattr(natural_dictation, "polish", polish)
    monkeypatch.setattr(dp, "paste_text", paste)
    with api(monkeypatch) as c:
        r = c.post("/api/dictation/transcribe-and-paste", files={"file": ("audio.wav", b"fake", "audio/wav")})
        assert r.status_code == 200
        assert r.json()["recoverable"] and not r.json()["pasted"]
        rows = c.get("/api/dictation/history").json()["entries"]
        assert rows[0]["status"] == "paste_failed"
        assert rows[0]["raw_text"] == "Eh visita fausto"
        assert calls[0][1]["owner"] == "luis"
        c.patch("/api/dictation/history/" + rows[0]["id"], json={"text": "Visita Faustus", "learn": False}).raise_for_status()
        assert c.delete("/api/dictation/history/" + rows[0]["id"]).json()["deleted"]


def test_api_vocabulary_roundtrip_persists_and_deletes(monkeypatch):
    with api(monkeypatch) as c:
        r = c.post("/api/dictation/vocabulary", json={"heard": "fausto", "replacement": "Faustus"})
        assert r.status_code == 200
        entry = r.json()
        assert c.get("/api/dictation/vocabulary").json()["entries"] == [entry]
    # New client and SQLite connection, same native store.
    with api(monkeypatch) as c:
        assert c.get("/api/dictation/vocabulary").json()["entries"][0]["id"] == entry["id"]
        assert c.delete("/api/dictation/vocabulary/" + entry["id"]).json()["deleted"]
        assert not c.get("/api/dictation/vocabulary").json()["entries"]
