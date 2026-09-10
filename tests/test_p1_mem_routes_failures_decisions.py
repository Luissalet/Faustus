"""HTTP surface for MEM-03 (`/failures*`) and MEM-04 (`/decisions*`) —
routes/memory_engine_routes.py's ADD-ONLY additions this lote. Real FastAPI
app + TestClient against the actual router (COMUN.md rule 7), mirroring
tests/test_l29_memory_forget_correct.py's own fixtures.
"""

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


# ── MEM-03 ───────────────────────────────────────────────────────────────────

def test_a_single_failure_report_does_not_promote(client):
    out = client.post("/api/memory-engine/failures", json={
        "signature": "xyz crashes on empty input", "summary": "xyz crashes on empty input",
        "project": "p1", "test_ref": "tests/test_xyz.py::test_empty"})
    assert out.status_code == 200, out.text
    body = out.json()
    assert body["outcome"] == "registered"
    assert body["item"]["status"] == "deprecated"


def test_repeated_failures_with_a_test_ref_promote_over_http(client):
    from src import memory_failures
    last = None
    for _ in range(memory_failures.MIN_OCCURRENCES_TO_PROPOSE):
        last = client.post("/api/memory-engine/failures", json={
            "signature": "abc leaks a file handle", "summary": "abc leaks a file handle",
            "project": "p1", "test_ref": "tests/test_abc.py::test_no_leak"})
    assert last.json()["outcome"] == "promoted"

    listed = client.get("/api/memory-engine/failures", params={"project": "p1"})
    assert listed.status_code == 200
    # promoted, so no longer a listed CANDIDATE
    assert all("abc leaks a file handle" not in c["text"] for c in listed.json()["candidates"])


# ── MEM-04 ───────────────────────────────────────────────────────────────────

def test_decision_round_trip_over_http(client):
    created = client.post("/api/memory-engine/decisions", json={
        "text": "Use SQLite for the context store", "project": "p1",
        "alternatives": ["Postgres"], "artifact_refs": ["src/context_engine/store.py"]})
    assert created.status_code == 200, created.text
    decision_id = created.json()["decision"]["id"]

    listed = client.get("/api/memory-engine/decisions", params={"project": "p1"})
    assert any(d["id"] == decision_id for d in listed.json()["decisions"])

    invalidated = client.post(f"/api/memory-engine/decisions/{decision_id}/invalidate",
                              json={"reason": "premise changed"})
    assert invalidated.status_code == 200, invalidated.text
    assert invalidated.json()["decision"]["status"] == "deprecated"

    # gone from the default (current-truth) view, still in the full one
    current = client.get("/api/memory-engine/decisions", params={"project": "p1"})
    assert all(d["id"] != decision_id for d in current.json()["decisions"])
    full = client.get("/api/memory-engine/decisions",
                      params={"project": "p1", "include_invalidated": True})
    assert any(d["id"] == decision_id for d in full.json()["decisions"])


def test_invalidating_an_unknown_decision_is_404(client):
    out = client.post("/api/memory-engine/decisions/does-not-exist/invalidate",
                      json={"reason": "x"})
    assert out.status_code == 404
