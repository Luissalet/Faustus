"""Folding a long text into five levels of detail: parsing, the extractive level, generated levels and the API."""
import asyncio
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import text_fold as fold

LONG = """# Resultados

La prueba de carga terminó sin errores en el servidor principal. El tiempo medio de respuesta fue de 120 ms durante toda la prueba. Se enviaron 40.000 peticiones en total desde tres clientes. El último cliente se desconectó antes de tiempo por un fallo de red local. Conviene repetir la medición con la red estable.

```python
for client in clients:
    client.send()
    client.wait()
    client.close()
```

## Conclusiones

- Subir el límite de conexiones
- Repetir la prueba
- Medir la memoria
- Revisar los registros
- Avisar al equipo

Las conclusiones dependen de repetir la prueba con una red estable, porque el fallo de red afectó a un tercio de las peticiones medidas.
"""


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    import src.constants as constants
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    fold._LOCKS.clear()


def test_parse_builds_numbered_paragraphs_and_sections():
    doc = fold.build(LONG)
    assert doc["format"] == "faustus.fold/1"
    assert [(s["id"], s["title"]) for s in doc["sections"]] == [("s1", "Resultados"), ("s2", "Conclusiones")]
    assert [(p["id"], p["kind"]) for p in doc["paragraphs"]] == [
        ("p1", "heading"), ("p2", "text"), ("p3", "code"), ("p4", "heading"), ("p5", "list"), ("p6", "text")]
    # The span of every paragraph points at its exact original text.
    assert all(LONG[p["start"]:p["end"]] == p["text"] for p in doc["paragraphs"])
    assert doc["paragraphs"][2]["text"].count("```") == 2  # a fenced block stays whole


def test_level_two_is_literal_deterministic_and_shorter():
    first, second = fold.build(LONG), fold.build(LONG)
    assert first["paragraphs"] == second["paragraphs"]
    prose = first["paragraphs"][1]
    kept = prose["levels"]["2"]
    assert 0 < len(kept) < len(prose["text"])
    # Every kept sentence is copied word for word from the original paragraph.
    for sentence in fold._sentences(kept):
        assert sentence in prose["text"]
    # Code keeps a balanced fence; lists keep their first lines and say how many were left out.
    assert first["paragraphs"][2]["levels"]["2"].count("```") == 2
    assert "[… 2 líneas más]" in first["paragraphs"][4]["levels"]["2"]
    assert sum(len(p["levels"]["2"]) for p in first["paragraphs"]) < len(LONG)


def test_empty_and_oversized_text_are_refused():
    with pytest.raises(fold.FoldError):
        fold.build("   \n ")
    with pytest.raises(fold.FoldError):
        fold.build("x" * (fold.MAX_CHARS + 1))


def test_documents_are_stored_per_owner():
    doc = fold.open_document("alice", LONG)
    assert fold.open_document("alice", LONG)["id"] == doc["id"]
    assert fold.load("alice", doc["id"])["sha256"] == doc["sha256"]
    with pytest.raises(fold.FoldNotFound):
        fold.load("bob", doc["id"])
    with pytest.raises(fold.FoldNotFound):
        fold.load("alice", "fold_../../etc")


def fake_llm(calls):
    async def call(messages):
        calls.append(messages[0]["content"][:20])
        system, user = messages[0]["content"], messages[1]["content"]
        if "mapa de temas" in system:
            return 'Aquí va: [{"title": "Prueba de carga", "gist": "Salió bien salvo la red.", "sections": ["s1"]}]'
        if "UNA línea" in system:
            return "[s1] La carga salió bien salvo un cliente.\n[s2] Repetir con red estable."
        ids = [line.split("]")[0].lstrip("[") for line in user.split("\n\n") if line.startswith("[p")]
        return "\n".join(f"[{i}] idea de {i} — porque sí" for i in ids)
    return call


def test_generated_levels_are_cached_and_levels_one_and_two_need_no_model():
    async def go():
        doc = fold.open_document("alice", LONG)
        calls = []
        full = await fold.read("alice", doc["id"], 1)
        assert full["text"] == "\n\n".join(p["text"] for p in doc["paragraphs"]) and calls == []
        sentences = await fold.read("alice", doc["id"], 2)
        assert sentences["chars"] < full["chars"]
        key = await fold.read("alice", doc["id"], 3, llm=fake_llm(calls))
        assert "[p2]" not in key["text"] and "idea de p2 — porque sí" in key["text"]
        again = await fold.read("alice", doc["id"], 3, llm=fake_llm(calls))
        assert again["text"] == key["text"] and len(calls) == 1  # cached, no second model call
        lines = await fold.read("alice", doc["id"], 4, llm=fake_llm(calls))
        assert [i["text"] for i in lines["items"]] == ["La carga salió bien salvo un cliente.", "Repetir con red estable."]
        topics = await fold.read("alice", doc["id"], 5, llm=fake_llm(calls))
        assert [c["title"] for c in topics["items"]] == ["Prueba de carga", "Otros"]  # uncovered section is not lost
        only = await fold.read("alice", doc["id"], 2, section="s2")
        assert {i["section"] for i in only["items"]} == {"s2"}
        original = await fold.read("alice", doc["id"], 4, paragraph="p5")
        assert original["level"] == 1 and original["text"] == doc["paragraphs"][4]["text"]
        with pytest.raises(fold.FoldNotFound):
            await fold.read("alice", doc["id"], 2, section="s9")
    asyncio.run(go())


def test_model_failure_is_reported_never_replaced_by_a_stand_in():
    async def broken(messages):
        raise ConnectionError("down")

    async def go():
        doc = fold.open_document("alice", LONG)
        for level in (3, 4, 5):
            with pytest.raises(fold.FoldUnavailable):
                await fold.read("alice", doc["id"], level, llm=broken)
        assert fold.load("alice", doc["id"])["generated"] == []
        assert (await fold.read("alice", doc["id"], 2))["items"]  # the deterministic levels still work
        with pytest.raises(fold.FoldError):
            await fold.read("alice", doc["id"], 7)
    asyncio.run(go())


def test_artifact_source_folds_the_longest_string_field(monkeypatch):
    import src.tool_result_offload as offload
    stored = json.dumps({"output": "short", "full_text": LONG})
    monkeypatch.setattr(offload, "read_artifact_range", lambda artifact_id, owner: {"text": stored})
    assert fold.text_of_artifact("alice", "occ_x") == LONG
    assert fold.text_of_artifact("alice", "occ_x", "/output") == "short"

    def denied(artifact_id, owner):
        raise offload.ArtifactAccessDenied("no")
    monkeypatch.setattr(offload, "read_artifact_range", denied)
    with pytest.raises(fold.FoldNotFound):
        fold.text_of_artifact("bob", "occ_x")


@pytest.fixture
def client(monkeypatch):
    from routes import text_fold_routes as routes
    monkeypatch.setattr(routes, "_owner", lambda request: request.headers.get("x-test-owner", "alice"))
    app = FastAPI()
    app.include_router(routes.setup_text_fold_routes())
    return TestClient(app)


def test_api_folds_reads_and_isolates_owners(client):
    made = client.post("/api/fold", json={"text": LONG, "level": 2}).json()
    fold_id = made["document"]["id"]
    assert made["view"]["name"] == "sentences" and made["document"]["chars_by_level"]["2"] < made["document"]["chars_by_level"]["1"]
    assert client.get(f"/api/fold/{fold_id}", params={"level": 1, "paragraph": "p2"}).json()["items"][0]["paragraph"] == "p2"
    assert client.get(f"/api/fold/{fold_id}/document").json()["format"] == "faustus.fold/1"
    other = client.get(f"/api/fold/{fold_id}", params={"level": 2}, headers={"x-test-owner": "bob"})
    assert other.status_code == 404 and other.json()["error_class"] == "fold.not_found"
    assert client.post("/api/fold", json={}).json()["error_class"] == "fold.invalid"
    assert client.post("/api/fold", json={"text": LONG, "artifact_id": "occ_x"}).status_code == 400
    assert client.get(f"/api/fold/{fold_id}", params={"level": 9}).json()["error_class"] == "fold.invalid"


def test_api_reports_an_unreachable_model_as_503(client, monkeypatch):
    async def none():
        raise fold.FoldUnavailable("no language model endpoint is configured")
    monkeypatch.setattr(fold, "default_llm", lambda owner: none())
    response = client.post("/api/fold", json={"text": LONG, "level": 3})
    assert response.status_code == 503 and response.json()["error_class"] == "fold.model_unavailable"


def test_mcp_server_lists_both_tools_and_is_registered():
    from mcp_servers import text_fold_server
    from src.builtin_mcp import _BUILTIN_SERVERS
    names = [tool.name for tool in asyncio.run(text_fold_server.list_tools())]
    assert names == ["fold_text", "fold_read"]
    assert _BUILTIN_SERVERS["text_fold"][0] == "mcp_servers/text_fold_server.py"


def test_offloaded_tool_result_points_at_fold_read_and_folds_from_the_real_store(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from core import database
    from src import artifact_store, tool_result_offload as offload

    engine = create_engine("sqlite:///" + (tmp_path / "fold.db").as_posix())
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=engine))
    database.Base.metadata.create_all(engine)
    monkeypatch.setattr(artifact_store, "ARTIFACT_STORE_DIR", str(tmp_path / "store"))
    monkeypatch.setattr(offload, "get_setting", lambda key, default=None: False if key == "offload_search_enabled" else default)
    stored = offload.offload_if_oversized({"output": "short", "full_text": LONG * 3}, owner="alice",
                                          session_id="s", call_id="c", tool="shell", threshold_chars=500)
    artifact_id = stored["artifact_id"]
    assert f'fold_read(artifact_id="{artifact_id}"' in stored["offload_note"]
    text = fold.text_of_artifact("alice", artifact_id)
    assert text == LONG * 3
    summary = fold.summary(fold.open_document("alice", text))
    assert summary["chars_by_level"]["2"] < summary["chars_by_level"]["1"]
    with pytest.raises(fold.FoldNotFound):
        fold.text_of_artifact("bob", artifact_id)
    engine.dispose()
