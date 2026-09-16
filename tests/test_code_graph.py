"""Tests for `src/code_graph/` (R2, Reach wave): architecture/tracing/change
queries over `src.context_engine.code_index`'s resolved graph, plus the
tool/schema registration every code_graph_* tool needs (contract rule 2).

Fixture repo (tmp_path, real `git init`): three Python modules with a clean
call chain (`process` -> `load_user` -> `fetch_user`), one FastAPI route, a
class with inheritance (`User(Base)`), and one JS file with a `fetch(...)`
call — matching the wave contract's fixture description.
"""
from __future__ import annotations

import asyncio
import os
import subprocess

import pytest

from src import code_graph
from src import tool_execution as te
from src.context_engine import store as ce_store


MODELS_PY = '''"""Domain models."""


class Base:
    """Shared entity behaviour."""

    def describe(self) -> str:
        return "base"


class User(Base):
    """A user record."""

    def describe(self) -> str:
        return "user"
'''

API_PY = '''"""HTTP routes."""
from fastapi import APIRouter

from .service import process

router = APIRouter()


@router.get("/users/{user_id}")
def get_user(user_id: str):
    """Return one user."""
    return process(user_id)
'''

SERVICE_PY = '''"""Business logic — the traceable call chain."""


def process(user_id):
    """Entry point the route calls."""
    return load_user(user_id)


def load_user(user_id):
    return fetch_user(user_id)


def fetch_user(user_id):
    return {"id": user_id}
'''

APP_JS = '''function fetchUsers() {
  return fetch("/api/users");
}
'''


def _write(root, rel, text):
    path = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(path) or root, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def _git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)


@pytest.fixture()
def ce_db(tmp_path):
    ce_store.use_path(str(tmp_path / "ce.db"))
    try:
        yield
    finally:
        ce_store.use_path(None)


@pytest.fixture()
def repo(tmp_path, ce_db):
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "models.py", MODELS_PY)
    _write(root, "api.py", API_PY)
    _write(root, "service.py", SERVICE_PY)
    _write(root, "static/app.js", APP_JS)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial")
    return str(root)


@pytest.fixture()
def ws(repo):
    """Bind `repo` as the active turn workspace, the way `execute_tool_block`
    would -- the same guard `read_file`/grep are confined by."""
    ws_token = te._active_workspace.set(repo)
    roots_token = te._active_workspace_roots.set((repo,))
    try:
        yield repo
    finally:
        te._active_workspace.reset(ws_token)
        te._active_workspace_roots.reset(roots_token)


# ── index / search / trace / callers ────────────────────────────────────

def test_index_builds_symbols_and_edges(ws):
    result = code_graph.index(ws)
    assert result["exit_code"] == 0
    assert result["symbols"] > 0
    assert result["edges"] > 0
    assert result["scanned"] >= 4


def test_search_finds_symbols_by_name(ws):
    code_graph.index(ws)
    hit = code_graph.search_graph("load_user", workspace=ws)
    assert hit["exit_code"] == 0
    names = [m["qualname"] for m in hit["matches"]]
    assert any("load_user" in n for n in names)


def test_search_respects_kind_filter(ws):
    code_graph.index(ws)
    hit = code_graph.search_graph("User", kinds=("class",), workspace=ws)
    for m in hit["matches"]:
        assert m["kind"] == "class"


def test_trace_path_follows_resolved_calls(ws):
    code_graph.index(ws)
    trace = code_graph.trace_path("process", "fetch_user", workspace=ws)
    assert trace["exit_code"] == 0
    assert trace["found"] is True
    symbols_in_path = [n["symbol"] for n in trace["path"]]
    assert symbols_in_path[0].endswith("process")
    assert symbols_in_path[-1].endswith("fetch_user")
    assert trace["hops"] >= 1
    # every hop after the first carries a certainty label
    for node in trace["path"][1:]:
        assert node["certainty"] in ("exact", "static_inferred", "lexical")


def test_trace_path_reports_not_found_for_unrelated_symbols(ws):
    code_graph.index(ws)
    trace = code_graph.trace_path("fetch_user", "process", workspace=ws, max_depth=2)
    # fetch_user does not call back into process (no cycle in the fixture)
    assert trace["exit_code"] == 0
    assert trace["found"] is False


def test_callers_finds_the_route_handler(ws):
    code_graph.index(ws)
    result = code_graph.callers("process", workspace=ws)
    assert result["exit_code"] == 0
    paths = [h.get("path", "") for h in result["hits"] if h.get("resolved")]
    assert any("api.py" in p for p in paths)


def test_callees_finds_the_next_hop(ws):
    code_graph.index(ws)
    result = code_graph.callees("process", workspace=ws)
    quals = [h.get("qualname", "") for h in result["hits"] if h.get("resolved")]
    assert any("load_user" in q for q in quals)


# ── detect_changes ──────────────────────────────────────────────────────

def test_detect_changes_maps_diff_to_symbols_and_callers(ws):
    code_graph.index(ws)
    service_path = os.path.join(ws, "service.py")
    with open(service_path, "a", encoding="utf-8") as handle:
        handle.write("\n\ndef extra_step(user_id):\n    return user_id\n")
    changed = code_graph.detect_changes(ws, base_ref="HEAD")
    assert changed["exit_code"] == 0
    names = [c["symbol"] for c in changed["changed_symbols"]]
    assert any("extra_step" in n for n in names)


def test_detect_changes_with_no_diff_is_empty(ws):
    code_graph.index(ws)
    changed = code_graph.detect_changes(ws, base_ref="HEAD")
    assert changed["exit_code"] == 0
    assert changed["changed_symbols"] == []


# ── architecture ─────────────────────────────────────────────────────────

def test_architecture_reports_languages_routes_and_hotspots(ws):
    code_graph.index(ws)
    arch = code_graph.get_architecture(ws)
    assert arch["exit_code"] == 0
    assert "py" in arch["languages"]
    assert "js" in arch["languages"]
    assert arch["symbols"] > 0
    assert any("/users/{user_id}" in r["route"] for r in arch["routes"])
    assert isinstance(arch["hotspots"], list)


# ── snippet ──────────────────────────────────────────────────────────────

def test_snippet_returns_only_the_symbol_lines(ws):
    code_graph.index(ws)
    snip = code_graph.snippet("fetch_user", workspace=ws)
    assert snip["exit_code"] == 0
    assert "def fetch_user" in snip["output"]
    assert "def process" not in snip["output"]


# ── incremental reindex ────────────────────────────────────────────────

def test_reindex_is_incremental(ws):
    first = code_graph.index(ws)
    assert first["reindexed"] >= 4
    second = code_graph.index(ws)
    assert second["reindexed"] == 0  # nothing changed since the first pass
    with open(os.path.join(ws, "service.py"), "a", encoding="utf-8") as handle:
        handle.write("\n\ndef untouched():\n    return 1\n")
    third = code_graph.index(ws)
    assert third["reindexed"] == 1  # only the touched file was reparsed


# ── output size is bounded ─────────────────────────────────────────────

def test_search_output_is_bounded(ws):
    code_graph.index(ws)
    result = code_graph.search_graph("e", workspace=ws, limit=200, output_chars=200)
    assert len(result["output"]) <= 260  # cap + truncation notice


def test_architecture_output_is_bounded_by_default(ws):
    code_graph.index(ws)
    arch = code_graph.get_architecture(ws)
    assert len(arch["output"]) <= 4200


# ── workspace confinement ──────────────────────────────────────────────

def test_root_outside_workspace_is_rejected(ws, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    with pytest.raises(ValueError):
        code_graph.index(str(outside))


# ── auto-index hook never raises outside an event loop ─────────────────

def test_auto_index_noop_without_running_loop(ws):
    from src.code_graph.auto_index import maybe_auto_index
    assert maybe_auto_index(ws) is False  # no running loop in a sync test


def test_auto_index_schedules_inside_a_running_loop(ws, monkeypatch):
    from src.code_graph import auto_index

    monkeypatch.setattr(auto_index, "_last_kicked", {})

    async def _run():
        fired = auto_index.maybe_auto_index(ws)
        await asyncio.sleep(0.3)  # let the background thread finish
        return fired

    fired = asyncio.run(_run())
    assert fired is True


# ── tool/schema registration (contract rule 2) ─────────────────────────

_NAMES = ("code_graph_index", "code_graph_search", "code_graph_trace",
          "code_graph_changes", "code_graph_architecture", "code_graph_snippet")


def test_tools_are_registered_everywhere():
    from src.agent_tools import TOOL_HANDLERS, TOOL_TAGS
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    from src.tool_capabilities import TOOL_CAPABILITIES
    from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS
    from src.tool_index_examples import EXAMPLES

    schema_names = {s["function"]["name"] for s in FUNCTION_TOOL_SCHEMAS}
    for name in _NAMES:
        assert name in TOOL_HANDLERS, name
        assert name in TOOL_TAGS, name
        assert name in schema_names, name
        assert name in TOOL_CAPABILITIES, name
        assert name in BUILTIN_TOOL_DESCRIPTIONS, name
        assert len(EXAMPLES.get(name, [])) >= 2, name


def test_code_graph_search_tool_executor(ws):
    from src.agent_tools.code_graph_tools import CodeGraphSearchTool

    code_graph.index(ws)
    result = asyncio.run(CodeGraphSearchTool().execute('{"pattern": "load_user"}', {}))
    assert result["exit_code"] == 0
    assert "load_user" in result["output"]
