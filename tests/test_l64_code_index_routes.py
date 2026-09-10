"""Lote 64 — IDX-02 (`refresh(paths=...)`) and IDX-03 (hybrid `search()`)
exercised through their actual HTTP surface.

`routes/context_engine_routes.py::POST /code-index/refresh` and
`GET /code-index/search` call `src.context_engine.code_index` (not
`src.code_index` — a different module this same lote also owns; see that
module's own docstring on why the two are kept apart). Neither route had a
test at the HTTP level before this file. `src/code_index.py::refresh`
separately gained its OWN `paths=` targeted-refresh support in this lote,
for that module's own callers (IDX-02's acceptance text names
`src/code_index.py` directly) — covered here at the function level since
`routes/code_index_routes.py` (the HTTP surface for THAT module) is outside
this lote's file list; see the batch report's "Cambios necesarios en
ficheros ajenos" for the one-line wiring it still needs.
"""
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import middleware
from routes.context_engine_routes import setup_context_engine_routes
from src import code_index as flat_ci
from src.context_engine import cache, store
from src.context_engine import code_index as ce_ci

TOOL_HEADERS = {middleware.INTERNAL_TOOL_HEADER: middleware.INTERNAL_TOOL_TOKEN}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    store.use_path(str(tmp_path / "ce.db"))
    cache.reset_working_set()
    monkeypatch.setattr(middleware, "auth_disabled", lambda: True)

    app = FastAPI()

    @app.middleware("http")
    async def _as_luis(request, call_next):
        request.state.current_user = "luis"
        return await call_next(request)

    app.include_router(setup_context_engine_routes())
    try:
        yield TestClient(app)
    finally:
        store.use_path(None)
        cache.reset_working_set()


def write(root, rel, text):
    path = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(path) or root, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


@pytest.fixture()
def ce_store(tmp_path):
    store.use_path(str(tmp_path / "ce.db"))
    try:
        yield
    finally:
        store.use_path(None)


@pytest.fixture()
def workspace(tmp_path):
    root = str(tmp_path / "repo")
    os.makedirs(root, exist_ok=True)
    write(root, "a.py", "def alpha():\n    return 1\n")
    write(root, "b.py", "def beta():\n    return 2\n")
    write(root, "c.py", "def gamma():\n    return 3\n")
    return root


# ── IDX-02: `src/code_index.py::refresh(paths=...)` targeted reindex ───────

def test_refresh_with_paths_indexes_only_the_named_files(ce_store, workspace):
    out = flat_ci.refresh(workspace, paths=["a.py"], pause_on_pressure=False)
    assert out["scanned"] == 1
    assert out["reindexed"] == 1
    status = flat_ci.status(workspace)
    assert status["files"] == 1
    assert flat_ci.find_definition("alpha", workspace=workspace)


def test_refresh_with_paths_never_deletes_files_outside_the_named_set(ce_store, workspace):
    flat_ci.refresh(workspace, pause_on_pressure=False)  # full index: 3 files
    assert flat_ci.status(workspace)["files"] == 3

    out = flat_ci.refresh(workspace, paths=["a.py"], full=True, pause_on_pressure=False)
    assert out["removed"] == 0
    # b.py and c.py were never looked at by this targeted call; they must
    # still be indexed, not treated as "gone".
    assert flat_ci.status(workspace)["files"] == 3
    assert flat_ci.find_definition("beta", workspace=workspace)


def test_refresh_with_paths_accepts_absolute_paths_too(ce_store, workspace):
    abs_path = os.path.join(workspace, "b.py")
    out = flat_ci.refresh(workspace, paths=[abs_path], pause_on_pressure=False)
    assert out["reindexed"] == 1
    assert flat_ci.find_definition("beta", workspace=workspace)


def test_refresh_with_paths_skips_a_path_outside_the_workspace(ce_store, workspace, tmp_path):
    outside = str(tmp_path / "elsewhere.py")
    with open(outside, "w", encoding="utf-8") as handle:
        handle.write("def outside_fn():\n    return 0\n")
    out = flat_ci.refresh(workspace, paths=[outside], pause_on_pressure=False)
    assert out["scanned"] == 0
    assert out["reindexed"] == 0


def test_a_changed_function_via_targeted_refresh_updates_its_definition(ce_store, workspace):
    flat_ci.refresh(workspace, paths=["a.py"], pause_on_pressure=False)
    write(workspace, "a.py", "def alpha_renamed():\n    return 1\n")
    flat_ci.refresh(workspace, paths=["a.py"], pause_on_pressure=False)
    assert flat_ci.find_definition("alpha", workspace=workspace) == []
    assert flat_ci.find_definition("alpha_renamed", workspace=workspace)


@pytest.mark.asyncio
async def test_refresh_async_forwards_paths_too(ce_store, workspace):
    out = await flat_ci.refresh_async(workspace, paths=["a.py"], pause_on_pressure=False)
    assert out["reindexed"] == 1
    assert flat_ci.status(workspace)["files"] == 1


# ── HTTP: the routes that are actually wired (context_engine.code_index) ───

def test_refresh_route_accepts_paths_without_raising(client, workspace):
    """`context_engine.code_index.refresh` already supported `paths=` before
    this lote; this is the first HTTP-level test for the route at all."""
    resp = client.post("/api/context/code-index/refresh",
                       json={"workspace": workspace, "paths": ["a.py"]},
                       headers=TOOL_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["refresh"]["reindexed"] == 1


def test_search_route_returns_the_fused_hybrid_hits(client, workspace):
    client.post("/api/context/code-index/refresh", json={"workspace": workspace},
               headers=TOOL_HEADERS)
    resp = client.get("/api/context/code-index/search",
                      params={"query": "alpha", "workspace": workspace})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["count"] >= 1
    top = body["symbols"][0]
    assert top["qualname"] == "alpha"
    # IDX-03: each hit now says which retrieval lanes actually contributed.
    assert top["tier"] in ("lexical", "hybrid", "refined", "reranked")
    assert isinstance(top["lanes"], list) and top["lanes"]


def test_search_route_kinds_filter_round_trips_as_a_csv_query_param(client, workspace):
    client.post("/api/context/code-index/refresh", json={"workspace": workspace},
               headers=TOOL_HEADERS)
    resp = client.get("/api/context/code-index/search",
                      params={"query": "alpha", "workspace": workspace, "kinds": "function"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] >= 1
    assert {hit["kind"] for hit in body["symbols"]} == {"function"}


def test_direct_call_and_route_agree_on_the_same_ranking(client, workspace):
    """The route is a thin wrapper: what `ce_ci.search()` returns directly
    must be exactly what the HTTP layer serves, order included."""
    client.post("/api/context/code-index/refresh", json={"workspace": workspace},
               headers=TOOL_HEADERS)
    direct = ce_ci.search("beta", workspace=workspace)
    resp = client.get("/api/context/code-index/search",
                      params={"query": "beta", "workspace": workspace})
    via_http = resp.json()["symbols"]
    assert [h["qualname"] for h in direct] == [h["qualname"] for h in via_http]
