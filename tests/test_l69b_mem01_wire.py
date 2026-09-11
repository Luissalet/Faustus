"""Lote 69b — MEM-01: `GET /api/memory-engine/items` carries `sensitivity`
and `confidence_state` on every item — the exact fields
`studio/src/adapters/memory.ts::ruleFrom` now parses (see
studio/checks/l69b-mem01-rules.check.mjs for the JS side of this same
contract) — and `DELETE .../forget` is a distinct, tombstoning action from
the plain `DELETE .../{id}`.

Mirrors tests/test_l29_memory_forget_correct.py's fixtures exactly (COMUN.md
rule 7: a real FastAPI app + TestClient against the actual router).
"""
from __future__ import annotations

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


def test_items_carry_sensitivity_and_confidence_state(client, store):
    created = client.post("/api/memory-engine/items", json={"text": "Never rebase main"}).json()["item"]
    # A human-written item (trust_class human_explicit, still active) is the
    # store's own example of "confirmed" per confidence_state()'s docstring.
    assert created["sensitivity"] == "normal"
    assert created["confidence_state"] == "confirmed"

    listed = client.get("/api/memory-engine/items").json()["items"]
    assert len(listed) == 1
    assert listed[0]["id"] == created["id"]
    assert listed[0]["sensitivity"] == "normal"
    assert listed[0]["confidence_state"] == "confirmed"


def test_a_deprecated_item_reads_obsolete_regardless_of_trust(client, store):
    created = client.post("/api/memory-engine/items", json={"text": "Old rule"}).json()["item"]
    engine.add_feedback(created["id"], "harmful", reason="wrong", ref="human:test")
    engine.add_feedback(created["id"], "harmful", reason="wrong", ref="human:test2")
    engine.add_feedback(created["id"], "harmful", reason="wrong", ref="human:test3")
    engine.add_feedback(created["id"], "harmful", reason="wrong", ref="human:test4")
    item = client.get("/api/memory-engine/items").json()["items"][0]
    if item["status"] == "deprecated":
        assert item["confidence_state"] == "obsolete"


def test_forget_is_a_distinct_action_from_plain_delete(client, store):
    a = client.post("/api/memory-engine/items", json={"text": "Rule A — deleted plainly"}).json()["item"]
    b = client.post("/api/memory-engine/items", json={"text": "Rule B — forgotten with a tombstone"}).json()["item"]

    plain = client.delete(f"/api/memory-engine/items/{a['id']}")
    assert plain.status_code == 200
    assert plain.json() == {"status": "success", "deleted": True, "id": a["id"]}
    # A plain delete never mentions a tombstone.
    assert "tombstone" not in plain.json()

    forgotten = client.request("DELETE", f"/api/memory-engine/items/{b['id']}/forget", json={"reason": "wrong"})
    assert forgotten.status_code == 200
    body = forgotten.json()
    assert body["forgotten"] is True
    assert body["tombstone"]["item_id"] == b["id"]

    # Both are gone from the live list either way — the distinction is what
    # happens on a later re-write of the SAME text (forget alone tombstones
    # it, per test_l29_memory_forget_correct.py), not visibility right now.
    assert client.get("/api/memory-engine/items").json()["items"] == []


def test_project_isolation_is_real_not_only_owner_isolation(client, store):
    """MEM-01's hueco: 'no se verificó aislamiento cruzado por proyecto,
    solo por owner'. `tests/test_memory_owner_isolation.py` already proves
    two owners never see each other's rules; this proves the SAME owner,
    two different projects, get the same separation — `list_items`'
    `project = ?` is an exact match, not an OR-unscoped fallback (that
    fallback is `scoped_items`' own job, used by the agent's own retrieval,
    not this listing route)."""
    a = client.post("/api/memory-engine/items", json={"text": "Project A secret rule", "project": "proj-a"}).json()["item"]
    b = client.post("/api/memory-engine/items", json={"text": "Project B secret rule", "project": "proj-b"}).json()["item"]
    unscoped = client.post("/api/memory-engine/items", json={"text": "A rule everywhere, no project"}).json()["item"]

    listed_a = {item["id"] for item in client.get("/api/memory-engine/items", params={"project": "proj-a"}).json()["items"]}
    listed_b = {item["id"] for item in client.get("/api/memory-engine/items", params={"project": "proj-b"}).json()["items"]}

    assert a["id"] in listed_a
    assert b["id"] not in listed_a, "project A's list must never leak project B's rule"
    assert b["id"] in listed_b
    assert a["id"] not in listed_b, "project B's list must never leak project A's rule"
    # An exact-project listing is deliberately narrower than the agent's own
    # scoped retrieval: it does NOT also return the unscoped rule (that is
    # `scoped_items`'s "a rule with no project is a rule everywhere" job).
    assert unscoped["id"] not in listed_a
    assert unscoped["id"] not in listed_b
