"""POST /api/brain/entities — create-or-find an entity from outside, with
its sources and relations. Same disposable-store pattern as
tests/test_brain_routes.py."""

from __future__ import annotations

import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from routes import brain_routes  # noqa: E402
from src import memory_engine as engine  # noqa: E402
from src.brain import db as brain_db  # noqa: E402

OWNER = "alice"


@pytest.fixture(autouse=True)
def isolated(tmp_path_factory, monkeypatch):
    data_dir = tmp_path_factory.mktemp("data")
    monkeypatch.setattr(engine, "DATA_DIR", str(data_dir))
    engine.set_vector_store(None)
    engine.clear_injected()
    brain_db.use_dir(str(tmp_path_factory.mktemp("brain")))
    import src.constants as constants_mod
    monkeypatch.setattr(constants_mod, "DATA_DIR", str(data_dir))
    monkeypatch.setenv("AUTH_ENABLED", "false")
    yield
    engine.reset_vector_store()
    engine.clear_injected()
    brain_db.use_dir(None)


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(brain_routes, "effective_user", lambda request: request.headers.get("x-test-owner") or "")
    app = FastAPI()
    app.include_router(brain_routes.setup_brain_routes())
    return TestClient(app)


def _post(client, body, owner=OWNER):
    return client.post("/api/brain/entities", headers={"x-test-owner": owner}, json=body)


def test_creates_then_finds_the_same_entity(client):
    first = _post(client, {"name": "Marta Ibarra", "type": "person", "aliases": ["Marta"]})
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["created"] is True
    assert body["entity"]["type"] == "person" and body["entity"]["name"] == "Marta Ibarra"

    again = _post(client, {"name": "marta ibarra", "type": "person"})
    assert again.status_code == 200, again.text
    assert again.json()["created"] is False
    assert again.json()["entity"]["id"] == body["entity"]["id"]
    listed = client.get("/api/brain/entities", headers={"x-test-owner": OWNER}).json()["entities"]
    assert [e["name"] for e in listed] == ["Marta Ibarra"]


def test_summary_mentions_and_relations_are_idempotent(client):
    body = {"name": "Pedido 4471", "type": "event", "summary": "Zapatillas, en reparto",
            "mentions": ["note:Notes/Familia/Pedido 4471.md"],
            "relations": [{"rel": "related_to", "to": "Tienda Norte", "to_type": "organization"}]}
    one = _post(client, body).json()
    two = _post(client, body).json()
    assert one["entity"]["id"] == two["entity"]["id"]
    assert two["entity"]["summary"] == "Zapatillas, en reparto"
    assert two["entity"]["mentions"] == ["note:Notes/Familia/Pedido 4471.md"]
    assert len(two["relations"]) == 1 and one["relations"][0]["id"] == two["relations"][0]["id"]
    assert len(two["entity"]["relations"]) == 1

    shop = client.get("/api/brain/entities", headers={"x-test-owner": OWNER}, params={"q": "Tienda Norte"}).json()["entities"]
    assert len(shop) == 1 and shop[0]["type"] == "organization"
    # the note that supports the relation is cited on the other end too
    assert "note:Notes/Familia/Pedido 4471.md" in shop[0]["mentions"]


def test_a_later_body_updates_the_summary(client):
    _post(client, {"name": "Pedido 4471", "type": "event", "summary": "En reparto"})
    later = _post(client, {"name": "Pedido 4471", "summary": "Entregado"}).json()
    assert later["created"] is False and later["entity"]["summary"] == "Entregado"


def test_literal_value_relation_and_bad_input(client):
    ok = _post(client, {"name": "Marta Ibarra", "type": "person",
                        "relations": [{"rel": "trabaja en", "value": "Cordera Labs"}]})
    assert ok.status_code == 200, ok.text
    assert ok.json()["relations"][0]["rel"] == "works_at"
    assert _post(client, {"name": "  "}).status_code == 400
    assert _post(client, {"name": "Marta Ibarra", "relations": [{"rel": "knows"}]}).status_code == 400
    assert _post(client, {"name": "Marta Ibarra", "relations": "x"}).status_code == 400


def test_owners_do_not_share_entities(client):
    mine = _post(client, {"name": "Marta Ibarra", "type": "person"}).json()
    theirs = _post(client, {"name": "Marta Ibarra", "type": "person"}, owner="mallory").json()
    assert theirs["created"] is True and theirs["entity"]["id"] != mine["entity"]["id"]
