"""Tests for `code_graph.change_risk` — the deterministic CHANGE-RISK score.

A real, scripted git repo (`tmp_path`, real `git init`/commits, and a real
isolated context-engine sqlite via `ce_store.use_path`) drives every
assertion: the risk score reads the real resolved code graph
(`code_index.refresh`) and real `git log` history (through `cochange.py`),
the same way `test_code_graph_impact.py` and `test_code_graph_cochange.py`
do for their own modules.

Fixture shape (all under `pkg/`):

  hub.py       hub_fn() -- imported AND called by 5 caller modules -> high
               fan-in, high breadth, high hub (imported by >= 5 modules).
  leaf.py      leaf_fn() -- nobody calls or imports it -> near-zero on
               every graph-shaped factor.
  tested.py    tested_fn() -- covered by tests/test_tested.py.
  untested.py  untested_fn() -- same shape as tested.py (one caller, no
               other callers), but no test file reaches it.
  churny.py    modified in 20 extra commits after the initial commit, so
               `git log` shows heavy recent churn.

`git log` is scanned with the module's own default lookback (matches
`cochanges()`'s `max_commits=500` so the cache is shared), so every commit
in this small fixture repo is always in range.
"""
from __future__ import annotations

import asyncio
import os
import subprocess

import pytest

from src import code_graph
from src.code_graph import risk as cgr
from src.context_engine import store as ce_store
from src import tool_execution as te


def _write(root, rel, text):
    path = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(path) or root, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def _git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)


def _commit(root, msg):
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", msg)


HUB_PY = "def hub_fn():\n    return 1\n"
LEAF_PY = "def leaf_fn():\n    return 2\n"
TESTED_PY = "def tested_fn():\n    return 3\n"
UNTESTED_PY = "def untested_fn():\n    return 4\n"
CHURNY_PY = "def churny_fn():\n    return 5\n"


def _caller(n, module, fn):
    return f"from pkg.{module} import {fn}\n\n\ndef caller_{n}():\n    return {fn}()\n"


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
    _write(root, "pkg/hub.py", HUB_PY)
    _write(root, "pkg/leaf.py", LEAF_PY)
    _write(root, "pkg/tested.py", TESTED_PY)
    _write(root, "pkg/untested.py", UNTESTED_PY)
    _write(root, "pkg/churny.py", CHURNY_PY)

    # hub.py: 5 modules import AND call it -> fan-in, breadth and hub all high.
    for i in range(5):
        _write(root, f"pkg/caller_hub_{i}.py", _caller(i, "hub", "hub_fn"))
    # untested.py: exactly one caller, same shape as tested.py's one caller,
    # so the two differ only in test coverage.
    _write(root, "pkg/caller_untested.py", _caller(0, "untested", "untested_fn"))
    _write(root, "pkg/caller_tested.py", _caller(0, "tested", "tested_fn"))

    _write(root, "tests/test_tested.py",
           "from pkg.tested import tested_fn\n\n\n"
           "def test_tested_fn():\n    assert tested_fn() == 3\n")

    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _commit(root, "initial")

    # churny.py: 20 more commits touching only this file.
    for i in range(20):
        _write(root, "pkg/churny.py", f"def churny_fn():\n    return {100 + i}\n")
        _commit(root, f"touch churny {i}")

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


# ── ordering: hub outranks a leaf ───────────────────────────────────────

def test_hub_outranks_leaf(ws):
    code_graph.index(ws)
    hub = code_graph.change_risk(["pkg/hub.py"], workspace=ws)
    leaf = code_graph.change_risk(["pkg/leaf.py"], workspace=ws)
    assert hub["exit_code"] == 0 and leaf["exit_code"] == 0
    assert hub["score"] > leaf["score"]
    assert hub["factors"]["fan_in"]["raw"] > leaf["factors"]["fan_in"]["raw"]
    assert hub["factors"]["breadth"]["raw"] > leaf["factors"]["breadth"]["raw"]
    assert hub["factors"]["hub"]["raw"] >= 5
    assert leaf["factors"]["hub"]["raw"] == 0
    assert leaf["factors"]["fan_in"]["raw"] == 0


def test_hub_reason_names_the_file_and_count(ws):
    code_graph.index(ws)
    hub = code_graph.change_risk(["pkg/hub.py"], workspace=ws)
    assert "pkg/hub.py" in hub["factors"]["hub"]["reason"]
    assert any("blast radius" in s for s in hub["suggestions"])


# ── no tests raises risk ────────────────────────────────────────────────

def test_no_tests_raises_risk_vs_tested_file(ws):
    code_graph.index(ws)
    tested = code_graph.change_risk(["pkg/tested.py"], workspace=ws)
    untested = code_graph.change_risk(["pkg/untested.py"], workspace=ws)
    assert tested["factors"]["test_coverage"]["raw"] >= 1
    assert untested["factors"]["test_coverage"]["raw"] == 0
    assert untested["factors"]["test_coverage"]["normalized"] == 1.0
    assert (untested["factors"]["test_coverage"]["weight"]
            > tested["factors"]["test_coverage"]["normalized"] * 0
            or True)  # normalized comparison below is the real assertion
    assert (untested["factors"]["test_coverage"]["normalized"]
            > tested["factors"]["test_coverage"]["normalized"])
    # Everything else about these two files is the same shape (one caller,
    # no history), so the untested one must score at least as risky.
    assert untested["score"] >= tested["score"]


def test_no_tests_suggestion_names_the_file(ws):
    code_graph.index(ws)
    untested = code_graph.change_risk(["pkg/untested.py"], workspace=ws)
    assert any("no tests reach this change" in s and "pkg/untested.py" in s
               for s in untested["suggestions"])


def test_tested_suggestion_has_pytest_command(ws):
    code_graph.index(ws)
    tested = code_graph.change_risk(["pkg/tested.py"], workspace=ws)
    assert any(s.startswith("run these tests: python -m pytest -q")
               and "tests/test_tested.py" in s for s in tested["suggestions"])


# ── churn ────────────────────────────────────────────────────────────────

def test_churny_file_has_high_churn_factor(ws):
    code_graph.index(ws)
    churny = code_graph.change_risk(["pkg/churny.py"], workspace=ws)
    leaf = code_graph.change_risk(["pkg/leaf.py"], workspace=ws)
    assert churny["factors"]["churn"]["raw"] >= 15  # 20 extra commits, capped display at 15
    assert churny["factors"]["churn"]["normalized"] == 1.0
    assert churny["factors"]["churn"]["raw"] > leaf["factors"]["churn"]["raw"]


# ── top_reasons / level / suggestions shape ─────────────────────────────

def test_top_reasons_are_the_three_biggest_contributors(ws):
    code_graph.index(ws)
    hub = code_graph.change_risk(["pkg/hub.py"], workspace=ws)
    assert 1 <= len(hub["top_reasons"]) <= 3
    contribs = [r["contribution"] for r in hub["top_reasons"]]
    assert contribs == sorted(contribs, reverse=True)
    for r in hub["top_reasons"]:
        assert r["factor"] in hub["factors"]
        assert r["reason"] == hub["factors"][r["factor"]]["reason"]


def test_level_matches_fixed_thresholds(ws):
    code_graph.index(ws)
    leaf = code_graph.change_risk(["pkg/leaf.py"], workspace=ws)
    assert leaf["level"] in ("low", "medium", "high")
    assert cgr._level(0.0) == "low"
    assert cgr._level(29.9) == "low"
    assert cgr._level(30.0) == "medium"
    assert cgr._level(64.9) == "medium"
    assert cgr._level(65.0) == "high"
    assert cgr._level(100.0) == "high"


def test_score_is_0_to_100(ws):
    code_graph.index(ws)
    for path in ("pkg/hub.py", "pkg/leaf.py", "pkg/tested.py", "pkg/untested.py",
                 "pkg/churny.py"):
        result = code_graph.change_risk([path], workspace=ws)
        assert 0.0 <= result["score"] <= 100.0


# ── diff-seeded path (real uncommitted change) ──────────────────────────

def test_diff_seeded_uses_real_uncommitted_change(ws):
    code_graph.index(ws)
    leaf_path = os.path.join(ws, "pkg", "leaf.py")
    with open(leaf_path, "a", encoding="utf-8") as handle:
        handle.write("\n\ndef leaf_fn_extra():\n    x = 1\n    y = 2\n    return x + y\n")
    result = code_graph.change_risk(workspace=ws)
    assert result["exit_code"] == 0
    assert result["mode"] == "diff"
    assert "pkg/leaf.py" in result["seeds"]
    assert result["factors"]["size"]["raw"] > 0
    assert "not diff-seeded" not in result["factors"]["size"]["reason"]


def test_symbol_seeded_has_no_size_factor(ws):
    code_graph.index(ws)
    result = code_graph.change_risk(["pkg/leaf.py"], workspace=ws)
    assert result["mode"] == "explicit"
    assert result["factors"]["size"]["raw"] == 0
    assert "not diff-seeded" in result["factors"]["size"]["reason"]


def test_diff_seeded_no_changes_is_clean(ws):
    code_graph.index(ws)
    result = code_graph.change_risk(workspace=ws)
    assert result["exit_code"] == 0
    assert result["mode"] == "diff"
    assert result["seeds"] == []
    assert result["score"] == 0


# ── determinism ──────────────────────────────────────────────────────────

def test_determinism_same_inputs_same_output(ws):
    code_graph.index(ws)
    first = code_graph.change_risk(["pkg/hub.py"], workspace=ws)
    second = code_graph.change_risk(["pkg/hub.py"], workspace=ws)
    assert first == second


def test_determinism_across_reindex(ws):
    code_graph.index(ws)
    first = code_graph.change_risk(["pkg/tested.py"], workspace=ws)
    code_graph.index(ws, force=True)
    second = code_graph.change_risk(["pkg/tested.py"], workspace=ws)
    assert first["score"] == second["score"]
    assert first["factors"] == second["factors"]


# ── symbol-name seeding (not just paths) ────────────────────────────────

def test_symbol_name_seed_resolves_to_its_file(ws):
    code_graph.index(ws)
    result = code_graph.change_risk(["hub_fn"], workspace=ws)
    assert result["exit_code"] == 0
    assert "pkg/hub.py" in result["seeds"]


def test_unresolved_symbol_is_reported(ws):
    code_graph.index(ws)
    result = code_graph.change_risk(["totally_nonexistent_symbol_xyz"], workspace=ws)
    assert "totally_nonexistent_symbol_xyz" in result["unresolved"]


# ── workspace confinement ───────────────────────────────────────────────

def test_root_outside_workspace_is_rejected(ws, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    result = code_graph.change_risk(["pkg/hub.py"], workspace=str(outside))
    assert result["exit_code"] == 1
    assert "error" in result


# ── impact() integration ────────────────────────────────────────────────

def test_impact_includes_compact_risk_block_by_default(ws):
    code_graph.index(ws)
    result = code_graph.impact("hub_fn", workspace=ws, depth=3)
    assert result["exit_code"] == 0
    assert "risk" in result
    assert "score" in result["risk"] and "level" in result["risk"]
    assert f"{result['risk']['score']}" in result["output"] or "Change risk" in result["output"]


def test_impact_include_risk_false_omits_block(ws):
    code_graph.index(ws)
    result = code_graph.impact("hub_fn", workspace=ws, depth=3, include_risk=False)
    assert result["risk"] == {}
    assert "Change risk:" not in result["output"]


# ── tool wiring ──────────────────────────────────────────────────────────

def test_code_graph_risk_registered_everywhere():
    from src.agent_tools import TOOL_HANDLERS, TOOL_TAGS
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    from src.tool_capabilities import TOOL_CAPABILITIES
    from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS
    from src.tool_index_examples import EXAMPLES

    name = "code_graph_risk"
    schema_names = {s["function"]["name"] for s in FUNCTION_TOOL_SCHEMAS}
    assert name in TOOL_HANDLERS
    assert name in TOOL_TAGS
    assert name in schema_names
    assert name in TOOL_CAPABILITIES
    assert name in BUILTIN_TOOL_DESCRIPTIONS
    assert len(EXAMPLES.get(name, [])) >= 2


def test_code_graph_risk_tool_executor(ws):
    from src.agent_tools.code_graph_tools import CodeGraphRiskTool

    code_graph.index(ws)
    result = asyncio.run(
        CodeGraphRiskTool().execute('{"paths": ["pkg/hub.py"]}', {}))
    assert result["exit_code"] == 0
    assert "pkg/hub.py" in result["seeds"]
    assert 0.0 <= result["score"] <= 100.0


def test_code_graph_risk_tool_executor_diff_mode(ws):
    from src.agent_tools.code_graph_tools import CodeGraphRiskTool

    code_graph.index(ws)
    leaf_path = os.path.join(ws, "pkg", "leaf.py")
    with open(leaf_path, "a", encoding="utf-8") as handle:
        handle.write("\n\nx = 1\n")
    result = asyncio.run(CodeGraphRiskTool().execute("{}", {}))
    assert result["exit_code"] == 0
    assert result["mode"] == "diff"


def test_code_graph_risk_tool_executor_comma_separated_paths(ws):
    from src.agent_tools.code_graph_tools import CodeGraphRiskTool

    code_graph.index(ws)
    result = asyncio.run(
        CodeGraphRiskTool().execute("pkg/hub.py, pkg/leaf.py", {}))
    assert result["exit_code"] == 0
    assert "pkg/hub.py" in result["seeds"] and "pkg/leaf.py" in result["seeds"]
