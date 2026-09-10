"""L29 (integrates L26's "necessary in routes/memory_engine_routes.py" note):
DELETE /items/{id}/forget and POST /items/{id}/correct — the HTTP surface
for `src/memory_engine.py`'s existing `forget()`/`correct()` (MEM-02), which
had no route at all before this. Also: `ItemCreate` now accepts `type`/
`session_id`/`provenance`, forwarded to `add_item()`.

Uses a real FastAPI app + TestClient against the actual
`setup_memory_engine_routes()` router (COMUN.md rule 7), mirroring
tests/test_memory_engine.py's own `client`/`store` fixtures exactly so this
suite is isolated the same way.

Reverting the two new routes (or `ItemCreate`'s new fields) makes every test
below fail with 404/422 instead of the expected 200 body.
"""
from __future__ import annotations

import sys

import pytest

from src import memory_engine as engine


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "DATA_DIR", str(tmp_path))
    engine.set_vector_store(None)
    engine.clear_injected()
    yield tmp_path
    engine.reset_vector_store()
    engine.clear_injected()


@pytest.fixture()
def client(store, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core.middleware import require_admin
    from routes import memory_engine_routes

    monkeypatch.setattr(memory_engine_routes, "effective_user", lambda request: "luis")
    app = FastAPI()
    app.include_router(memory_engine_routes.setup_memory_engine_routes())
    app.dependency_overrides[require_admin] = lambda: None
    return TestClient(app)


def test_forget_tombstones_the_item_and_it_stops_resurrecting(client, store):
    created = client.post("/api/memory-engine/items", json={"text": "Never rebase main"}).json()["item"]
    forgotten = client.request("DELETE", f"/api/memory-engine/items/{created['id']}/forget",
                               json={"reason": "no longer true"})
    assert forgotten.status_code == 200
    body = forgotten.json()
    assert body["forgotten"] is True
    assert body["tombstone"]["item_id"] == created["id"]
    assert body["tombstone"]["reason"] == "no longer true"
    # Gone from the live list...
    assert client.get("/api/memory-engine/items").json()["items"] == []
    # ...and MEM-02's whole point: the same text does not silently resurrect
    # through a later agent_assertion write (only an explicit human one can).
    # Same owner/project/level the route created the item with (it defaults
    # level to "procedural") — the tombstone key is scoped by all three.
    with pytest.raises(engine.MemoryEngineError, match="forgotten"):
        engine.add_item("Never rebase main", owner="luis", level="procedural",
                        trust_class="agent_assertion")


def test_forget_an_unknown_item_is_404(client, store):
    assert client.delete("/api/memory-engine/items/nope/forget").status_code == 404


def test_correct_replaces_the_item_and_links_back(client, store):
    created = client.post("/api/memory-engine/items", json={"text": "Tests live in test/"}).json()["item"]
    corrected = client.post(f"/api/memory-engine/items/{created['id']}/correct",
                            json={"text": "Tests live in tests/", "reason": "typo"})
    assert corrected.status_code == 200
    new_item = corrected.json()["item"]
    assert new_item["text"] == "Tests live in tests/"
    assert new_item["id"] != created["id"]
    assert new_item["provenance"]["corrected_from"] == created["id"]
    # The original is gone (tombstoned, same as forget), replaced by the one item.
    listed = client.get("/api/memory-engine/items").json()["items"]
    assert [i["id"] for i in listed] == [new_item["id"]]


def test_correct_an_unknown_item_is_404(client, store):
    assert client.post("/api/memory-engine/items/nope/correct", json={"text": "x"}).status_code == 404


def test_correct_rejects_empty_text(client, store):
    created = client.post("/api/memory-engine/items", json={"text": "A rule"}).json()["item"]
    assert client.post(f"/api/memory-engine/items/{created['id']}/correct",
                       json={"text": "   "}).status_code == 400
    # The original must survive an invalid correction attempt untouched.
    assert client.get("/api/memory-engine/items").json()["items"][0]["id"] == created["id"]


def test_item_create_exposes_type_session_scope_and_provenance(client, store):
    created = client.post("/api/memory-engine/items", json={
        "text": "Prefer ruff over flake8",
        "type": "preference",
        "session_id": "sess-42",
        "provenance": {"source": "onboarding doc"},
    }).json()["item"]
    assert created["type"] == "preference"
    assert created["scope"] == "session:sess-42"
    assert created["provenance"]["source"] == "onboarding doc"


def test_item_create_without_the_new_fields_behaves_as_before(client, store):
    """Additive: omitting type/session_id/provenance must not change the
    existing default behavior (COMUN.md back-compat rule)."""
    created = client.post("/api/memory-engine/items", json={"text": "Keep functions short"}).json()["item"]
    assert created["scope"] == "global"
    # add_item() always stamps provenance.who=<trust_class> even with no
    # provenance kwarg supplied — that default predates this lote and must
    # not change; the new field only adds keys on top of it (see the
    # positive-case test above).
    assert created["provenance"] == {"who": "human_explicit"}
