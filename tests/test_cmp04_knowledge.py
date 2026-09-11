"""tests/test_cmp04_knowledge.py — CMP-04 (CONTRATO_CMP_W2.md, W2-D).

`src/knowledge_neighborhood.py::neighborhood()` -- the typed
requirement -> decision -> symbol -> test -> run view, built entirely out of
reads against `src/requirements/` (store + evidence matrix),
`src/project_board.py` and `src/context_engine/code_index.py`, no second
store of record.

The decisive scenario from the ficha: a requirement changes AFTER its
evidence was verified, while the code it implements is untouched ("test
antiguo en verde, código sin actualizar") -- the neighborhood must mark that
evidence edge `stale` and must NOT report the requirement as `verified`
(`test_decisive_stale_evidence_is_never_reported_verified`).

Same fixture shape as `tests/test_adp18_requirements.py` (module-level
`src.requirements`/`src.project_board` functions, isolated by pointing
`DATA_DIR` at a fresh `tmp_path` per test) and
`tests/test_context_engine_derived_sources.py` (`context_engine.store.use_path`
for the code index's own sqlite file).
"""
from __future__ import annotations

import hashlib
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import projects as projects_mod  # noqa: E402
from src import constants as constants_mod  # noqa: E402
from src import knowledge_neighborhood  # noqa: E402
from src import project_board  # noqa: E402
from src.context_engine import code_index  # noqa: E402
from src.context_engine import store as ce_store  # noqa: E402
from src.requirements import store as req_store  # noqa: E402

OWNER = "luis"
PROJECT = "proj-1"


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path_factory, monkeypatch):
    """Requirements and the board both resolve `default_path()` off
    `DATA_DIR` (see their own module docstrings) -- one tmp dir isolates
    both stores for every test in this file, no per-module monkeypatch
    needed the way `test_adp18_requirements.py` does for a few of its own
    tests that build a `Store` against an explicit path instead."""
    data_dir = tmp_path_factory.mktemp("data")
    monkeypatch.setattr(constants_mod, "DATA_DIR", str(data_dir))
    yield


@pytest.fixture(autouse=True)
def isolated_code_index(tmp_path):
    ce_store.use_path(str(tmp_path / "ce.db"))
    yield
    ce_store.use_path(None)


@pytest.fixture()
def project_store(tmp_path, monkeypatch):
    """`neighborhood()` resolves a project's workspace through
    `services.projects.get_store()` (the same call
    `routes/requirements_routes.py::_project_or_404` makes) -- an isolated
    store here, same shape `test_adp18_requirements.py::project_store` uses
    for the requirement tools' own tests."""
    st = projects_mod.ProjectStore(str(tmp_path / "projects_data"))
    monkeypatch.setattr(projects_mod, "_store", st)
    monkeypatch.setattr(projects_mod, "get_store", lambda: st)
    return st


@pytest.fixture()
def workspace(tmp_path):
    ws = tmp_path / "workspace"
    (ws / "src").mkdir(parents=True)
    (ws / "tests").mkdir(parents=True)
    return ws


def _real_project(project_store, workspace_path) -> str:
    """`neighborhood()` resolves `workspace` by looking the project up in the
    projects store (the same call `_project_or_404` makes in
    `routes/requirements_routes.py`) -- tests that need `implemented`/
    `tested` to actually resolve must register a real project first, unlike
    `req_store`/`project_board`, which take any `project_id` string as-is.
    Returns the store-assigned id, used as `project_id` for the rest of that
    test (never the plain `PROJECT` constant the other tests use)."""
    row = project_store.create(name="Faustus", folder="Faustus", workspace=str(workspace_path),
                                owner=OWNER, scaffold_memory=False)
    return row["id"]


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _content_hash(abs_path) -> str:
    return hashlib.sha256(abs_path.read_bytes()).hexdigest()


def _link(project_id, key, *, kind, target, workspace_path="", revision=""):
    """`add_link` with a content hash computed the same way
    `evidence.link_evidence` computes it, for `implements`/`tests` targets --
    bypassing `link_evidence` itself (which requires a real, resolvable
    workspace path at call time) so a test can set up a link and only THEN
    write or rewrite the file it points at, the way
    `test_adp18_requirements.py::_link` already does."""
    content_hash = None
    if kind in ("implements", "tests") and workspace_path:
        path = target.split("@")[0].split("::")[0]
        abs_path = workspace_path / path
        if abs_path.is_file():
            content_hash = _content_hash(abs_path)
    return req_store.add_link(project_id, key, kind=kind, target=target,
                               revision=revision, content_hash=content_hash)


# ---------------------------------------------------------------------------
# id desconocido -- nunca inventado
# ---------------------------------------------------------------------------
def test_unknown_req_key_is_explicit():
    result = knowledge_neighborhood.neighborhood(PROJECT, OWNER, req_key="REQ-999")
    assert result["unknown"] == ["REQ-999"]
    assert result["nodes"] == []
    assert result["edges"] == []


def test_empty_call_is_a_well_formed_empty_neighborhood():
    """No `path`/`req_key`/`issue_key` -- never "every requirement in the
    project" standing in for scope."""
    req_store.create(PROJECT, title="Unrelated requirement")
    result = knowledge_neighborhood.neighborhood(PROJECT, OWNER)
    assert result["nodes"] == []
    assert result["edges"] == []


# ---------------------------------------------------------------------------
# requirement -> decision(issue) -> symbol -> test, all `declared`
# ---------------------------------------------------------------------------
def test_declared_edges_from_requirement_links(project_store, workspace):
    _write(workspace / "src" / "auth.py", "def login(user, pw):\n    return True\n")
    _write(workspace / "tests" / "test_auth.py", "def test_login():\n    assert True\n")
    project_id = _real_project(project_store, workspace)
    issue = project_board.create_issue(project_id, "FAU", type="feature", title="Add login")

    r = req_store.create(project_id, title="Users can log in", acceptance=["works"])
    key = r["key"]
    _link(project_id, key, kind="implements", target="src/auth.py@login", workspace_path=workspace)
    _link(project_id, key, kind="tests", target="tests/test_auth.py@test_login", workspace_path=workspace)
    req_store.add_link(project_id, key, kind="issue", target=issue["id"])

    result = knowledge_neighborhood.neighborhood(project_id, OWNER, req_key=key, depth=1)
    nodes_by_id = {n["id"]: n for n in result["nodes"]}

    req_node = nodes_by_id[f"requirement:{key}"]
    assert req_node["type"] == "requirement"
    assert req_node["stale"] is False
    assert req_node["implemented"] is True
    assert req_node["tested"] is True

    decision_node = nodes_by_id[f"decision:{issue['id']}"]
    assert decision_node["type"] == "decision"
    assert decision_node["stale"] is False

    symbol_node = nodes_by_id["symbol:src/auth.py@login"]
    assert symbol_node["type"] == "symbol"
    test_node = nodes_by_id["test:tests/test_auth.py@test_login"]
    assert test_node["type"] == "test"

    by_relation_kind = {(e["relation"], e["kind"]) for e in result["edges"]}
    assert ("declared", "issue") in by_relation_kind
    assert ("declared", "implements") in by_relation_kind
    assert ("declared", "tests") in by_relation_kind
    for edge in result["edges"]:
        assert edge["stale"] is False


# ---------------------------------------------------------------------------
# La prueba decisiva de la ficha: requisito cambiado + test antiguo en verde
# + código sin actualizar -> stale, nunca "verified".
# ---------------------------------------------------------------------------
def test_decisive_stale_evidence_is_never_reported_verified(project_store, workspace):
    _write(workspace / "src" / "auth.py", "def login(user, pw):\n    return True\n")
    _write(workspace / "tests" / "test_auth.py", "def test_login():\n    assert True\n")
    project_id = _real_project(project_store, workspace)

    r = req_store.create(project_id, title="Users can log in")
    key = r["key"]
    _link(project_id, key, kind="implements", target="src/auth.py@login", workspace_path=workspace)
    _link(project_id, key, kind="tests", target="tests/test_auth.py@test_login", workspace_path=workspace)
    evidence_link = _link(project_id, key, kind="evidences", target="run_123")
    assert evidence_link["req_revision_at_link"] == 1

    # The requirement changes; the code and the (green) test it names do not.
    req_store.update(project_id, key, {"text": "changed after verification"}, by="human")

    result = knowledge_neighborhood.neighborhood(project_id, OWNER, req_key=key)
    nodes_by_id = {n["id"]: n for n in result["nodes"]}

    req_node = nodes_by_id[f"requirement:{key}"]
    assert req_node["verified"] is False, "a stale evidences link must never leave the requirement verified"
    assert req_node["stale"] is True
    # The code itself is untouched -- implemented/tested stay true; only the
    # evidence went stale, and the neighborhood must say so, not less.
    assert req_node["implemented"] is True
    assert req_node["tested"] is True

    evidence_edges = [e for e in result["edges"] if e["kind"] == "evidences"]
    assert len(evidence_edges) == 1
    edge = evidence_edges[0]
    assert edge["relation"] == "verified"
    assert edge["stale"] is True
    assert "evidencia caducada" in edge["why"]

    run_node = nodes_by_id[edge["dst"]]
    assert run_node["type"] == "run"
    assert run_node["stale"] is True
    assert key in result["stale_refs"] or run_node["ref"] in result["stale_refs"]


def test_deleted_evidence_target_is_unknown_not_stale(workspace):
    """`evidence.py`'s own distinction ("borrar test -> degrada") carries
    through the neighborhood unchanged: a target gone entirely is `unknown`,
    never conflated with `stale`."""
    _write(workspace / "tests" / "test_auth.py", "def test_login():\n    assert True\n")
    r = req_store.create(PROJECT, title="Users can log in")
    key = r["key"]
    _link(PROJECT, key, kind="tests", target="tests/test_auth.py@test_login", workspace_path=workspace)
    os.remove(workspace / "tests" / "test_auth.py")

    result = knowledge_neighborhood.neighborhood(PROJECT, OWNER, req_key=key)
    edges = [e for e in result["edges"] if e["kind"] == "tests"]
    assert len(edges) == 1
    assert edges[0]["stale"] is False  # unknown, not stale


# ---------------------------------------------------------------------------
# path -> requirement (declared link matched by file prefix)
# ---------------------------------------------------------------------------
def test_path_resolves_the_requirement_that_implements_it(workspace):
    _write(workspace / "src" / "auth.py", "def login(user, pw):\n    return True\n")
    r = req_store.create(PROJECT, title="Users can log in")
    key = r["key"]
    _link(PROJECT, key, kind="implements", target="src/auth.py@login", workspace_path=workspace)

    result = knowledge_neighborhood.neighborhood(PROJECT, OWNER, path="src/auth.py")
    assert f"requirement:{key}" in {n["id"] for n in result["nodes"]}


# ---------------------------------------------------------------------------
# decision found by text mention, never presented as a declared link
# ---------------------------------------------------------------------------
def test_mentioning_issue_is_located_not_declared():
    issue = project_board.create_issue(
        PROJECT, "FAU", type="bug", title="src/auth.py leaks a session token",
    )
    result = knowledge_neighborhood.neighborhood(PROJECT, OWNER, path="src/auth.py")
    decision_edges = [e for e in result["edges"] if e["dst"] == f"decision:{issue['id']}"]
    assert decision_edges, "an issue mentioning the path must be found by text search"
    assert decision_edges[0]["relation"] == "located"
    assert decision_edges[0]["kind"] == "mentions"


# ---------------------------------------------------------------------------
# code index: structural neighbors are `located`, never `declared`
# ---------------------------------------------------------------------------
def test_code_index_neighbors_are_located(workspace):
    _write(workspace / "src" / "auth.py", "def login():\n    return True\n")
    _write(
        workspace / "src" / "app.py",
        "from src.auth import login\n\ndef handle():\n    return login()\n",
    )
    code_index.refresh(str(workspace), project_id=PROJECT)

    result = knowledge_neighborhood.neighborhood(PROJECT, OWNER, path="src/app.py", depth=1)
    assert result["nodes"], "the code index must contribute at least the seed symbols"
    for edge in result["edges"]:
        if edge["relation"] != "located":
            continue
        assert edge["kind"], "a located code-index edge must carry the graph's own edge kind"


def test_depth_is_clamped_to_max():
    result = knowledge_neighborhood.neighborhood(PROJECT, OWNER, path="src/x.py", depth=99)
    assert result["depth"] == knowledge_neighborhood.MAX_DEPTH
