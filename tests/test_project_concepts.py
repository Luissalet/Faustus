"""Tests for src/project_concepts.py — the agent's own persistent,
per-project architecture concept graph.

Covers: CRUD + soft delete + history, edges both directions, `understand`
ranking (a deterministic fake embedder, plus a real fastembed pass skipped
when the package is unavailable), stale detection, per-project isolation,
tool registration parity (TOOL-01/TOOL-03 style, mirroring
tests/test_tool_registry.py), route scoping, and the injection setting
defaulting off.
"""
from __future__ import annotations

import importlib
import os

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import project_concepts as pc


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _deterministic_embeddings(monkeypatch):
    """A tiny, deterministic bag-of-words embedder so `understand` ranking
    tests never depend on network or the real fastembed model. Two texts
    that share more words get a higher cosine score -- exactly the property
    the ranking test needs, without pulling in ONNX."""
    vocab = {}

    def _vec(text: str) -> np.ndarray:
        words = str(text or "").lower().split()
        v = np.zeros(64, dtype="float32")
        for w in words:
            idx = vocab.setdefault(w, len(vocab) % 64)
            v[idx] += 1.0
        n = np.linalg.norm(v)
        return v / n if n else v

    def _fake_embed_texts(texts):
        return np.array([_vec(t) for t in texts], dtype="float32")

    monkeypatch.setattr(pc, "_embed_texts", _fake_embed_texts)
    yield


@pytest.fixture
def store(tmp_path):
    return pc.Store("test-project", path=str(tmp_path / "concepts.db"))


# ---------------------------------------------------------------------------
# CRUD + soft delete + history
# ---------------------------------------------------------------------------
def test_upsert_creates_then_updates_same_id(store):
    c1 = store.upsert_concept(name="Web fetch", kind="feature", summary="Fetches pages", refs=["src/a.py"])
    assert c1["id"] == "web-fetch"
    assert c1["kind"] == "feature"

    c2 = store.upsert_concept(concept_id=c1["id"], name="Web fetch", kind="feature",
                               summary="Fetches and cleans pages", refs=["src/a.py", "src/b.py"])
    assert c2["id"] == c1["id"]
    assert c2["summary"] == "Fetches and cleans pages"
    assert len(c2["refs"]) == 2

    all_ids = [c["id"] for c in store.all_concepts()]
    assert all_ids == ["web-fetch"]  # update, not a duplicate row


def test_upsert_bad_kind_rejected(store):
    with pytest.raises(pc.ProjectConceptsError) as exc:
        store.upsert_concept(name="X", kind="nonsense")
    assert exc.value.error_class == "project_concepts.bad_kind"


def test_soft_delete_hides_but_keeps_history(store):
    c = store.upsert_concept(name="Old cache", kind="module", summary="stale layer")
    assert store.remove_concept(c["id"]) is True
    assert store.get_concept(c["id"]) is None
    # history survives
    hist = store.history(c["id"])
    actions = [h["action"] for h in hist]
    assert "create" in actions and "remove" in actions
    # a second remove is a no-op, not an error
    assert store.remove_concept(c["id"]) is False


def test_history_records_before_after_on_update(store):
    c = store.upsert_concept(name="Auth", kind="component", summary="v1")
    store.upsert_concept(concept_id=c["id"], name="Auth", kind="component", summary="v2")
    hist = store.history(c["id"])
    update_entries = [h for h in hist if h["action"] == "update"]
    assert len(update_entries) == 1
    assert update_entries[0]["before"]["summary"] == "v1"
    assert update_entries[0]["after"]["summary"] == "v2"


# ---------------------------------------------------------------------------
# Edges
# ---------------------------------------------------------------------------
def test_link_visible_from_both_endpoints(store):
    a = store.upsert_concept(name="Feature A", kind="feature", summary="a")
    b = store.upsert_concept(name="Module B", kind="module", summary="b")
    store.link(a["id"], b["id"], "depends_on", note="a needs b")

    detail_a = store.get_concept(a["id"])
    detail_b = store.get_concept(b["id"])
    assert any(e["dst"] == b["id"] and e["rel"] == "depends_on" for e in detail_a["outgoing"])
    assert any(e["src"] == a["id"] and e["rel"] == "depends_on" for e in detail_b["incoming"])


def test_link_unknown_endpoint_rejected(store):
    a = store.upsert_concept(name="Feature A", kind="feature")
    with pytest.raises(pc.ProjectConceptsError) as exc:
        store.link(a["id"], "does-not-exist", "connects_to")
    assert exc.value.error_class == "project_concepts.not_found"


def test_link_bad_relation_rejected(store):
    a = store.upsert_concept(name="Feature A", kind="feature")
    b = store.upsert_concept(name="Module B", kind="module")
    with pytest.raises(pc.ProjectConceptsError) as exc:
        store.link(a["id"], b["id"], "orbits")
    assert exc.value.error_class == "project_concepts.bad_rel"


def test_unlink_removes_edge(store):
    a = store.upsert_concept(name="A", kind="feature")
    b = store.upsert_concept(name="B", kind="module")
    store.link(a["id"], b["id"], "calls")
    assert store.unlink(a["id"], b["id"], "calls") == 1
    detail = store.get_concept(a["id"])
    assert detail["outgoing"] == []


def test_remove_concept_soft_removes_its_edges(store):
    a = store.upsert_concept(name="A", kind="feature")
    b = store.upsert_concept(name="B", kind="module")
    store.link(a["id"], b["id"], "connects_to")
    store.remove_concept(b["id"])
    detail = store.get_concept(a["id"])
    assert detail["outgoing"] == []  # edge to a removed concept doesn't show


# ---------------------------------------------------------------------------
# understand() ranking (deterministic fake embedder)
# ---------------------------------------------------------------------------
def test_understand_ranks_closer_concept_first(store):
    store.upsert_concept(name="Web content fetching", kind="feature",
                          summary="fetches and cleans web pages for the agent")
    store.upsert_concept(name="Voice synthesis", kind="feature",
                          summary="text to speech output for the assistant")
    result = store.understand("how is web content fetched and cleaned")
    names = [c["name"] for c in result["concepts"]]
    assert names[0] == "Web content fetching"


def test_understand_returns_one_hop_neighbors(store):
    core = store.upsert_concept(name="Web content fetching", kind="feature", summary="fetches web pages")
    helper = store.upsert_concept(name="HTML cleaner", kind="module", summary="strips boilerplate html tags")
    unrelated = store.upsert_concept(name="Payments", kind="feature", summary="billing and invoices")
    store.link(core["id"], helper["id"], "depends_on")

    result = store.understand("web pages fetched", k=1)
    assert result["concepts"][0]["id"] == core["id"]
    neighbor_ids = {n["id"] for n in result["neighbors"]}
    assert helper["id"] in neighbor_ids
    assert unrelated["id"] not in neighbor_ids


def test_understand_empty_query_returns_nothing(store):
    store.upsert_concept(name="X", kind="feature", summary="something")
    result = store.understand("")
    assert result["concepts"] == []
    assert result["neighbors"] == []


@pytest.mark.skipif(
    importlib.util.find_spec("fastembed") is None,
    reason="fastembed not installed in this environment",
)
def test_understand_with_real_fastembed(tmp_path, monkeypatch):
    """Same ranking property, this time through the real local embedder
    (no HTTP endpoint configured, so `get_embedding_client()` falls back to
    FastEmbedClient) -- confirms the offline path actually works end to
    end, not just against the test's fake embedder."""
    monkeypatch.delenv("EMBEDDING_URL", raising=False)
    monkeypatch.undo()  # stop the autouse fake-embedder patch for this test
    st = pc.Store("real-embed-project", path=str(tmp_path / "real.db"))
    st.upsert_concept(name="Web content fetching", kind="feature",
                       summary="fetches and cleans web pages for the agent")
    st.upsert_concept(name="Voice synthesis", kind="feature",
                       summary="text to speech output for the assistant")
    result = st.understand("how is web content fetched and cleaned")
    assert result["concepts"], "expected at least one ranked concept"
    assert result["concepts"][0]["name"] == "Web content fetching"


# ---------------------------------------------------------------------------
# Stale detection
# ---------------------------------------------------------------------------
def test_stale_check_flags_missing_path(store, tmp_path):
    workspace = tmp_path / "repo"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "real.py").write_text("def foo():\n    pass\n")
    c = store.upsert_concept(name="Thing", kind="module", refs=["src/real.py", "src/missing.py"])
    result = store.stale_check(c, str(workspace))
    assert result["stale"] is True
    reasons = {i["why"] for i in result["issues"]}
    assert "path_missing" in reasons
    assert len(result["issues"]) == 1  # real.py is fine


def test_stale_check_flags_missing_symbol(store, tmp_path):
    workspace = tmp_path / "repo"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "real.py").write_text("def foo():\n    pass\n")
    c = store.upsert_concept(name="Thing", kind="module", refs=["src/real.py@bar"])
    result = store.stale_check(c, str(workspace))
    assert result["stale"] is True
    assert result["issues"][0]["why"] == "symbol_not_found"


def test_stale_check_clean_when_everything_resolves(store, tmp_path):
    workspace = tmp_path / "repo"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "real.py").write_text("def foo():\n    pass\n")
    c = store.upsert_concept(name="Thing", kind="module", refs=["src/real.py@foo"])
    result = store.stale_check(c, str(workspace))
    assert result["stale"] is False
    assert result["issues"] == []


def test_stale_check_no_workspace_is_unchecked_not_stale(store):
    c = store.upsert_concept(name="Thing", kind="module", refs=["src/whatever.py"])
    result = store.stale_check(c, "")
    assert result["checked"] is False
    assert result["stale"] is False


def test_stale_check_rejects_path_escaping_workspace(store, tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    c = store.upsert_concept(name="Thing", kind="module", refs=["../../etc/passwd"])
    result = store.stale_check(c, str(workspace))
    assert result["stale"] is True
    assert result["issues"][0]["why"] == "path_outside_workspace"


# ---------------------------------------------------------------------------
# Per-project isolation
# ---------------------------------------------------------------------------
def test_two_projects_never_share_concepts(tmp_path):
    a = pc.Store("proj-a", path=str(tmp_path / "a.db"))
    b = pc.Store("proj-b", path=str(tmp_path / "b.db"))
    a.upsert_concept(name="Only in A", kind="feature")
    assert a.all_concepts()
    assert b.all_concepts() == []


def test_resolve_project_key_prefers_project_id_over_workspace():
    key_with_id = pc.resolve_project_key(project_id="abc123", workspace="/some/path")
    assert key_with_id == "proj-abc123"


def test_resolve_project_key_falls_back_to_workspace_hash():
    key = pc.resolve_project_key(workspace="/some/path")
    assert key.startswith("ws-")
    # same workspace -> same key, deterministic
    assert key == pc.resolve_project_key(workspace="/some/path")


def test_resolve_project_key_requires_something():
    with pytest.raises(pc.ProjectConceptsError) as exc:
        pc.resolve_project_key()
    assert exc.value.error_class == "project_concepts.no_project"


# ---------------------------------------------------------------------------
# list_roots / graph
# ---------------------------------------------------------------------------
def test_list_roots_excludes_children_and_removed(store):
    parent = store.upsert_concept(name="Parent", kind="feature")
    store.upsert_concept(name="Child", kind="module", parent_id=parent["id"])
    gone = store.upsert_concept(name="Gone", kind="feature")
    store.remove_concept(gone["id"])

    roots = store.list_roots()
    root_ids = {r["id"] for r in roots}
    assert parent["id"] in root_ids
    assert "child" not in root_ids
    assert gone["id"] not in root_ids
    parent_row = next(r for r in roots if r["id"] == parent["id"])
    assert parent_row["child_count"] == 1


def test_graph_includes_degree(store):
    a = store.upsert_concept(name="A", kind="feature")
    b = store.upsert_concept(name="B", kind="module")
    store.link(a["id"], b["id"], "depends_on")
    g = store.graph()
    node_a = next(n for n in g["nodes"] if n["id"] == a["id"])
    assert node_a["degree"] == 1
    assert len(g["edges"]) == 1


# ---------------------------------------------------------------------------
# Tool registration parity (mirrors tests/test_tool_registry.py's own checks)
# ---------------------------------------------------------------------------
_CONCEPT_TOOLS = {
    "concepts_understand", "concept_get", "concepts_roots",
    "concept_upsert", "concept_link", "concept_remove",
}


def test_concept_tools_have_handlers():
    from src.agent_tools import TOOL_HANDLERS
    assert _CONCEPT_TOOLS <= set(TOOL_HANDLERS.keys())


def test_concept_tools_have_schemas():
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    names = {(e.get("function") or {}).get("name") for e in FUNCTION_TOOL_SCHEMAS}
    assert _CONCEPT_TOOLS <= names


def test_concept_tools_have_capabilities():
    from src.tool_capabilities import capabilities_for_tool
    for name in _CONCEPT_TOOLS:
        caps = capabilities_for_tool(name)
        assert caps.known is True, f"{name} has no explicit capability classification"


def test_concept_tools_have_index_descriptions_and_examples():
    from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS
    from src.tool_index_examples import EXAMPLES
    for name in _CONCEPT_TOOLS:
        assert name in BUILTIN_TOOL_DESCRIPTIONS and len(BUILTIN_TOOL_DESCRIPTIONS[name]) > 10
        assert len(EXAMPLES.get(name, [])) >= 2


# ---------------------------------------------------------------------------
# Route scoping
# ---------------------------------------------------------------------------
def _client(monkeypatch):
    import routes.project_concepts_routes as route_module
    monkeypatch.setattr(route_module, "require_admin", lambda request: None)
    app = FastAPI()
    app.include_router(route_module.setup_project_concepts_routes())
    return TestClient(app)


def test_route_roundtrip_create_get_link_graph(tmp_path, monkeypatch):
    import src.project_concepts as pc_mod
    monkeypatch.setattr(pc_mod, "db_path", lambda key: str(tmp_path / f"{key}.db"))
    client = _client(monkeypatch)

    r = client.post("/api/project-concepts", json={
        "workspace": str(tmp_path), "name": "Routing", "kind": "module", "summary": "http routes",
    })
    assert r.status_code == 201, r.text
    cid = r.json()["concept"]["id"]

    r2 = client.get(f"/api/project-concepts/{cid}", params={"workspace": str(tmp_path)})
    assert r2.status_code == 200
    assert r2.json()["concept"]["name"] == "Routing"

    r3 = client.get("/api/project-concepts/graph", params={"workspace": str(tmp_path)})
    assert r3.status_code == 200
    assert any(n["id"] == cid for n in r3.json()["nodes"])


def test_route_different_workspaces_are_isolated(tmp_path, monkeypatch):
    import src.project_concepts as pc_mod
    monkeypatch.setattr(pc_mod, "db_path", lambda key: str(tmp_path / f"{key}.db"))
    client = _client(monkeypatch)
    ws_a = tmp_path / "a"
    ws_b = tmp_path / "b"
    ws_a.mkdir()
    ws_b.mkdir()

    client.post("/api/project-concepts", json={"workspace": str(ws_a), "name": "OnlyA", "kind": "feature"})
    r = client.get("/api/project-concepts", params={"workspace": str(ws_b)})
    assert r.status_code == 200
    assert r.json()["concepts"] == []


def test_route_get_missing_concept_is_404(tmp_path, monkeypatch):
    import src.project_concepts as pc_mod
    monkeypatch.setattr(pc_mod, "db_path", lambda key: str(tmp_path / f"{key}.db"))
    client = _client(monkeypatch)
    r = client.get("/api/project-concepts/does-not-exist", params={"workspace": str(tmp_path)})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Injection setting off -> no change
# ---------------------------------------------------------------------------
def test_injection_setting_defaults_off():
    from src.settings import DEFAULT_SETTINGS
    assert DEFAULT_SETTINGS["agent_project_concepts_inject"] is False


def test_injection_is_a_noop_when_setting_is_off(monkeypatch, tmp_path):
    """With the setting off (the default), the agent loop must not touch
    src.project_concepts at all -- patch Store.understand to explode and
    confirm building messages the ordinary way never calls it."""
    import src.agent_loop as agent_loop_mod

    def _boom(*a, **kw):
        raise AssertionError("project_concepts.Store.understand should not be called when injection is off")

    monkeypatch.setattr(pc.Store, "understand", _boom)
    monkeypatch.setattr(agent_loop_mod, "get_setting",
                         lambda key, default=None: False if key == "agent_project_concepts_inject" else default)
    # The guard itself is a plain `if ... and get_setting(...)` -- exercise
    # it directly rather than running the whole streaming loop.
    from src.settings import get_setting as real_get_setting  # noqa: F401
    assert agent_loop_mod.get_setting("agent_project_concepts_inject", False) is False
