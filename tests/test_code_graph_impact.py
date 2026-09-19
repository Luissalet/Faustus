"""Tests for `code_graph.impact` — "what else can break and which tests to
run", from a symbol or from the current git diff.

Two layers, deliberately:

1. BFS mechanics (depth grouping, depth cap, weakest-certainty propagation
   across two paths to the same node, unresolved edges counted but never
   followed, node dedupe) are tested against a monkeypatched
   `code_index.neighbors` so the graph shape is exact and deterministic —
   the real extractor (`src.context_engine.code_index`) deliberately *drops*
   an ambiguous call rather than recording it unresolved (see
   `test_an_ambiguous_call_is_dropped_rather_than_guessed` in
   `test_context_engine_code_index.py`), so there is no way to produce a
   real unresolved `calls` edge from source text alone.
2. End-to-end behaviour (symbol seeding, diff seeding via a real `git
   diff`, affected_tests/suggested_command, tool wiring) runs against a
   real tiny project indexed the same way `test_code_graph.py` does.
"""
from __future__ import annotations

import asyncio
import os
import subprocess

import pytest

from src import code_graph
from src.code_graph import query as cgq
from src import tool_execution as te
from src.context_engine import code_index
from src.context_engine import store as ce_store


# ── monkeypatched BFS mechanics ─────────────────────────────────────────

def _hop(symbol_id, *, direction="in", resolved=True, certainty="static_inferred",
         qualname="", path="", start_line=0):
    return {
        "symbol_id": symbol_id, "direction": direction, "resolved": resolved,
        "certainty": certainty, "qualname": qualname, "path": path,
        "start_line": start_line, "edge_kind": "calls",
    }


def _fake_graph_neighbors(monkeypatch):
    """seed <-A- A <-B/C- B/C <-D- D, plus an unresolved caller of seed and
    an 'out' edge everywhere that must never be followed. B is reachable via
    two paths (A -> B lexical, C -> B exact) so the weaker one must win."""
    graph = {
        "seed": [
            _hop("A", certainty="static_inferred", qualname="pkg.a.A", path="pkg/a.py", start_line=10),
            _hop("unresolved-caller", resolved=False),
            _hop("dummy", direction="out", certainty="exact", qualname="whatever", path="x.py", start_line=1),
        ],
        "A": [
            _hop("B", certainty="lexical", qualname="pkg.b.B", path="pkg/b.py", start_line=20),
            _hop("C", certainty="static_inferred", qualname="pkg.c.C", path="pkg/c.py", start_line=5),
        ],
        "C": [
            _hop("B", certainty="exact", qualname="pkg.b.B", path="pkg/b.py", start_line=20),
        ],
        "B": [
            _hop("D", certainty="static_inferred", qualname="tests.test_thing.test_function",
                 path="tests/test_thing.py", start_line=3),
        ],
    }

    def fake_neighbors(symbol_id, *, kinds=(), hops=1):
        assert kinds == ("calls",)
        return graph.get(symbol_id, [])

    monkeypatch.setattr(code_index, "neighbors", fake_neighbors)
    monkeypatch.setattr(code_index, "refresh", lambda *a, **kw: {"scanned": 0})
    monkeypatch.setattr(
        cgq, "_resolve_symbol",
        lambda name, *, root, project_id: {"id": "seed", "qualname": "pkg.seed.run",
                                           "path": "pkg/seed.py", "start_line": 1},
    )


def test_impact_depth_grouping_and_cap(tmp_path, monkeypatch):
    _fake_graph_neighbors(monkeypatch)
    root = str(tmp_path)
    result = code_graph.impact("run", workspace=root, depth=2)
    assert result["exit_code"] == 0
    by_symbol = {n["symbol"]: n for n in result["reached"]}
    assert by_symbol["pkg.a.A"]["depth"] == 1
    assert by_symbol["pkg.b.B"]["depth"] == 2
    assert by_symbol["pkg.c.C"]["depth"] == 2
    # D is only reachable at depth 3 — depth=2 must not reach it.
    assert "tests.test_thing.test_function" not in by_symbol


def test_impact_depth_cap_extends_reach(tmp_path, monkeypatch):
    _fake_graph_neighbors(monkeypatch)
    root = str(tmp_path)
    result = code_graph.impact("run", workspace=root, depth=3)
    by_symbol = {n["symbol"]: n for n in result["reached"]}
    assert by_symbol["tests.test_thing.test_function"]["depth"] == 3


def test_impact_depth_is_capped_at_six(tmp_path, monkeypatch):
    _fake_graph_neighbors(monkeypatch)
    root = str(tmp_path)
    result = code_graph.impact("run", workspace=root, depth=999)
    assert result["depth"] == 6


def test_impact_weakest_certainty_wins_across_paths(tmp_path, monkeypatch):
    _fake_graph_neighbors(monkeypatch)
    root = str(tmp_path)
    result = code_graph.impact("run", workspace=root, depth=3)
    by_symbol = {n["symbol"]: n for n in result["reached"]}
    # B is reached via A (lexical) and via C (exact) — the weaker of the two
    # (lexical) must be kept, never silently upgraded.
    assert by_symbol["pkg.b.B"]["certainty"] == "lexical"
    # A and C are each reached by exactly one path, unaffected.
    assert by_symbol["pkg.a.A"]["certainty"] == "static_inferred"
    assert by_symbol["pkg.c.C"]["certainty"] == "static_inferred"


def test_impact_unresolved_edges_counted_not_followed(tmp_path, monkeypatch):
    _fake_graph_neighbors(monkeypatch)
    root = str(tmp_path)
    result = code_graph.impact("run", workspace=root, depth=3)
    assert result["unresolved_edges"] == 1
    symbols = {n["symbol"] for n in result["reached"]}
    assert "unresolved-caller" not in symbols
    assert not any(s == "" for s in symbols)  # nothing from the unresolved hop leaked in


def test_impact_dedupes_nodes(tmp_path, monkeypatch):
    _fake_graph_neighbors(monkeypatch)
    root = str(tmp_path)
    result = code_graph.impact("run", workspace=root, depth=3)
    names = [n["symbol"] for n in result["reached"]]
    assert len(names) == len(set(names))  # B counted once despite two paths


def test_impact_collects_affected_tests_and_suggested_command(tmp_path, monkeypatch):
    _fake_graph_neighbors(monkeypatch)
    root = str(tmp_path)
    result = code_graph.impact("run", workspace=root, depth=3)
    assert result["affected_tests"] == ["tests/test_thing.py"]
    assert result["affected_test_functions"] == ["tests.test_thing.test_function"]
    assert result["suggested_command"] == "python -m pytest -q tests/test_thing.py"
    assert "tests/test_thing.py" in result["output"]


def test_impact_never_raises_on_symbol_not_found(tmp_path, monkeypatch):
    monkeypatch.setattr(cgq, "_resolve_symbol", lambda *a, **kw: None)
    monkeypatch.setattr(code_index, "refresh", lambda *a, **kw: {"scanned": 0})
    result = code_graph.impact("nope", workspace=str(tmp_path))
    assert result["exit_code"] == 1
    assert "not found" in result["output"]


# ── real fixture project: symbol seeding + diff seeding ────────────────

A_PY = '''"""a calls b."""
from .b import b


def a():
    return b()
'''

B_PY = '''"""b calls c."""
from .c import c


def b():
    return c()
'''

C_PY = '''"""leaf of the chain."""


def c():
    return 1
'''

TEST_PY = '''from pkg.a import a


def test_a():
    assert a() == 1
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
    _write(root, "pkg/__init__.py", "")
    _write(root, "pkg/a.py", A_PY)
    _write(root, "pkg/b.py", B_PY)
    _write(root, "pkg/c.py", C_PY)
    _write(root, "tests/test_a.py", TEST_PY)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial")
    return str(root)


@pytest.fixture()
def ws(repo):
    ws_token = te._active_workspace.set(repo)
    roots_token = te._active_workspace_roots.set((repo,))
    try:
        yield repo
    finally:
        te._active_workspace.reset(ws_token)
        te._active_workspace_roots.reset(roots_token)


def test_impact_from_symbol_reaches_transitive_caller_and_test(ws):
    code_graph.index(ws)
    result = code_graph.impact("c", workspace=ws, depth=3)
    assert result["exit_code"] == 0
    symbols = {n["symbol"] for n in result["reached"]}
    assert "b" in symbols
    assert "a" in symbols
    assert result["affected_tests"] == ["tests/test_a.py"]
    assert result["suggested_command"] == "python -m pytest -q tests/test_a.py"


def test_impact_from_symbol_with_depth_one_misses_the_transitive_caller(ws):
    code_graph.index(ws)
    result = code_graph.impact("c", workspace=ws, depth=1)
    symbols = {n["symbol"] for n in result["reached"]}
    assert "b" in symbols
    assert "a" not in symbols


def test_impact_seed_itself_in_a_test_file_includes_its_own_test_file(ws):
    code_graph.index(ws)
    result = code_graph.impact("test_a", workspace=ws)
    assert "tests/test_a.py" in result["affected_tests"]


def test_impact_diff_seeded_reaches_the_test_touching_the_changed_symbol(ws):
    code_graph.index(ws)
    c_path = os.path.join(ws, "pkg", "c.py")
    with open(c_path, "w", encoding="utf-8") as handle:
        handle.write(C_PY.replace("return 1", "return 2"))  # edit c() itself
    result = code_graph.impact(workspace=ws, base_ref="HEAD", depth=3)
    assert result["exit_code"] == 0
    assert result["mode"] == "diff"
    assert len(result["seeds"]) >= 1
    assert result["affected_tests"] == ["tests/test_a.py"]


def test_impact_diff_seeded_with_no_changes_is_empty(ws):
    code_graph.index(ws)
    result = code_graph.impact(workspace=ws, base_ref="HEAD")
    assert result["exit_code"] == 0
    assert result["seeds"] == []
    assert result["affected_tests"] == []


def test_impact_output_is_bounded(ws):
    code_graph.index(ws)
    result = code_graph.impact("c", workspace=ws, output_chars=50)
    assert len(result["output"]) <= 120


def test_impact_root_outside_workspace_is_rejected(ws, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    result = code_graph.impact("c", workspace=str(outside))
    assert result["exit_code"] == 1
    assert "error" in result


# ── tool wiring ──────────────────────────────────────────────────────────

def test_code_graph_impact_registered_everywhere():
    from src.agent_tools import TOOL_HANDLERS, TOOL_TAGS
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    from src.tool_capabilities import TOOL_CAPABILITIES
    from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS
    from src.tool_index_examples import EXAMPLES

    name = "code_graph_impact"
    schema_names = {s["function"]["name"] for s in FUNCTION_TOOL_SCHEMAS}
    assert name in TOOL_HANDLERS
    assert name in TOOL_TAGS
    assert name in schema_names
    assert name in TOOL_CAPABILITIES
    assert name in BUILTIN_TOOL_DESCRIPTIONS
    assert len(EXAMPLES.get(name, [])) >= 2


def test_code_graph_impact_tool_executor(ws):
    from src.agent_tools.code_graph_tools import CodeGraphImpactTool

    code_graph.index(ws)
    result = asyncio.run(CodeGraphImpactTool().execute('{"symbol": "c", "depth": 3}', {}))
    assert result["exit_code"] == 0
    assert "tests/test_a.py" in result["affected_tests"]


def test_code_graph_impact_tool_executor_diff_mode(ws):
    from src.agent_tools.code_graph_tools import CodeGraphImpactTool

    code_graph.index(ws)
    c_path = os.path.join(ws, "pkg", "c.py")
    with open(c_path, "a", encoding="utf-8") as handle:
        handle.write("\n\ndef extra():\n    return 2\n")
    result = asyncio.run(CodeGraphImpactTool().execute("{}", {}))
    assert result["exit_code"] == 0
    assert result["mode"] == "diff"
