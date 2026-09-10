"""routes/code_index_routes.py — the find_symbol/callers/tests_for HTTP
surface (Lote 38, IDX-02/IDX-03).

Mounted on its own FastAPI app with auth disabled, following the same
pattern tests/test_context_engine_routes.py uses for its own router: this is
not the running application (routes/code_index_routes.py is not yet wired
into app.py — see this lote's final report), but it is TestClient against
the real router and real handlers, not a mock of either.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes.code_index_routes import setup_code_index_routes
from src.context_engine import store


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from core import middleware

    store.use_path(str(tmp_path / "ce.db"))
    monkeypatch.setattr(middleware, "auth_disabled", lambda: True)

    app = FastAPI()
    app.include_router(setup_code_index_routes())
    try:
        yield TestClient(app)
    finally:
        store.use_path(None)


def _write(root, rel, text):
    import os
    path = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(path) or root, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def test_reindex_then_symbol_lookup_returns_definition_callers_and_tests(client, tmp_path):
    root = str(tmp_path / "proj")
    _write(root, "app/util.py", "def helper():\n    return 1\n\n\ndef caller():\n    return helper()\n")
    _write(root, "tests/test_util.py", "from app.util import helper\n\n\ndef test_helper():\n    assert helper() == 1\n")

    resp = client.post("/api/code-index/proj1/reindex", json={"path": root})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["refresh"]["reindexed"] == 2

    resp = client.get("/api/code-index/proj1/symbol", params={"q": "helper", "path": root})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["definitions"][0]["qualname"] == "helper"
    assert any(hit["path"] == "app/util.py" for hit in body["callers"])
    assert any(hit["path"] == "tests/test_util.py" for hit in body["tests"])


def test_symbol_lookup_requires_q(client, tmp_path):
    resp = client.get("/api/code-index/proj1/symbol", params={"path": str(tmp_path)})
    assert resp.status_code == 400


def test_reindex_scopes_by_project_id(client, tmp_path):
    """Reindexing under `proj1` writes rows scoped to `proj1` only - a
    second project_id over the same physical path starts empty until it is
    itself reindexed (the scope key `src.code_index` isolates on)."""
    from src import code_index as ci

    root = str(tmp_path / "proj")
    _write(root, "a.py", "def only_here():\n    return 1\n")

    resp = client.post("/api/code-index/proj1/reindex", json={"path": root})
    assert resp.json()["refresh"]["reindexed"] == 1

    assert ci.status(root, project_id="proj1")["symbols"] == 1
    assert ci.status(root, project_id="proj2")["symbols"] == 0
