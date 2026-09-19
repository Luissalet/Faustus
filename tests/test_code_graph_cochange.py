"""Tests for `code_graph.cochanges` — the historical co-change signal, and
its wiring into `impact()` and the tool registries.

A real, scripted git repo (`tmp_path`, real `git init`/`git commit`) drives
every assertion here: `cochanges` reads real `git log` output, so there is
no useful way to fake the graph shape the way `test_code_graph_impact.py`
does for BFS mechanics.

Commit timeline (oldest -> newest), by short label:

  setup             -- config.yaml, package-lock.json (no a.py yet)
  init              -- add a.py
  c_early           -- modify a.py, add early_pair.py      (pair support=1, OLD)
  b                 -- modify a.py, add a_test.py           (pair support=1 of 2)
  c                 -- modify a.py, modify config.yaml      (pair support=1)
  d                 -- modify a.py, modify a_test.py         (pair support=2 of 2)
  huge              -- modify a.py + 60 new files            (must be SKIPPED)
  lockonly          -- modify a.py, modify package-lock.json (lockfile filtered)
  g                 -- modify a.py, add helper.py            (pair support=1 of 2)
  h                 -- modify a.py, remove helper.py         (pair support=2 of 2)
  recent_pair_commit-- modify a.py, add recent_pair.py       (pair support=1, NEW)

So, scanning history for "a.py": 9 commits touch it (setup does not; huge is
dropped), giving `target_commit_count == 9`. `a_test.py` and `helper.py`
each have support 2; `config.yaml`, `early_pair.py` and `recent_pair.py`
each have support 1; `package-lock.json` never appears (noise); none of the
huge commit's 60 files ever appear (commit dropped, not just capped).
"""
from __future__ import annotations

import asyncio
import os
import subprocess

import pytest

from src import code_graph
from src.code_graph import cochange as cgc
from src import tool_execution as te


def _write(root, rel, text=""):
    path = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(path) or root, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def _rm(root, rel):
    os.remove(os.path.join(root, *rel.split("/")))


def _git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)


def _commit(root, msg):
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", msg)
    proc = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root,
                          check=True, capture_output=True, text=True)
    return proc.stdout.strip()


@pytest.fixture()
def repo(tmp_path):
    root = str(tmp_path / "repo")
    os.makedirs(root)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")

    shas = {}
    _write(root, "config.yaml", "v1\n")
    _write(root, "package-lock.json", "{}\n")
    shas["setup"] = _commit(root, "setup")

    _write(root, "a.py", "def a():\n    return 1\n")
    shas["init"] = _commit(root, "init a.py")

    _write(root, "a.py", "def a():\n    return 2\n")
    _write(root, "early_pair.py", "x = 1\n")
    shas["c_early"] = _commit(root, "c_early")

    _write(root, "a.py", "def a():\n    return 3\n")
    _write(root, "a_test.py", "def test_a():\n    assert True\n")
    shas["b"] = _commit(root, "b")

    _write(root, "a.py", "def a():\n    return 4\n")
    _write(root, "config.yaml", "v2\n")
    shas["c"] = _commit(root, "c")

    _write(root, "a.py", "def a():\n    return 5\n")
    _write(root, "a_test.py", "def test_a():\n    assert True  # updated\n")
    shas["d"] = _commit(root, "d")

    _write(root, "a.py", "def a():\n    return 6\n")
    for i in range(60):
        _write(root, f"bulk/o{i}.py", f"x = {i}\n")
    shas["huge"] = _commit(root, "huge rename/format pass")

    _write(root, "a.py", "def a():\n    return 7\n")
    _write(root, "package-lock.json", '{"v": 2}\n')
    shas["lockonly"] = _commit(root, "lockonly")

    _write(root, "a.py", "def a():\n    return 8\n")
    _write(root, "helper.py", "y = 1\n")
    shas["g"] = _commit(root, "g")

    _write(root, "a.py", "def a():\n    return 9\n")
    _rm(root, "helper.py")
    shas["h"] = _commit(root, "h")

    _write(root, "a.py", "def a():\n    return 10\n")
    _write(root, "recent_pair.py", "z = 1\n")
    shas["recent_pair_commit"] = _commit(root, "recent_pair_commit")

    cgc._CACHE.clear()
    cgc._CACHE_ORDER.clear()
    return root, shas


@pytest.fixture()
def ws(repo):
    root, shas = repo
    ws_token = te._active_workspace.set(root)
    roots_token = te._active_workspace_roots.set((root,))
    try:
        yield root, shas
    finally:
        te._active_workspace.reset(ws_token)
        te._active_workspace_roots.reset(roots_token)


# ── core mining ──────────────────────────────────────────────────────────

def test_cochanges_support_confidence_and_ranking(ws):
    root, shas = ws
    result = code_graph.cochanges("a.py", workspace=root, min_support=1, limit=20)
    assert result["exit_code"] == 0
    assert result["target_commit_count"] == 9  # setup excluded, huge dropped
    by_path = {r["path"]: r for r in result["results"]}

    assert by_path["a_test.py"]["support"] == 2
    assert by_path["helper.py"]["support"] == 2
    assert by_path["config.yaml"]["support"] == 1
    assert by_path["early_pair.py"]["support"] == 1
    assert by_path["recent_pair.py"]["support"] == 1

    assert by_path["a_test.py"]["confidence"] == pytest.approx(2 / 9, abs=1e-4)
    assert by_path["config.yaml"]["confidence"] == pytest.approx(1 / 9, abs=1e-4)

    # ranked: support-2 files first (score also higher), support-1 files after.
    order = [r["path"] for r in result["results"]]
    assert order.index("a_test.py") < order.index("config.yaml")
    assert order.index("helper.py") < order.index("early_pair.py")


def test_cochanges_recency_weighting_breaks_ties_at_equal_support(ws):
    root, shas = ws
    result = code_graph.cochanges("a.py", workspace=root, min_support=1, limit=20)
    by_path = {r["path"]: r for r in result["results"]}
    # recent_pair.py (newest commit) and early_pair.py (oldest) both have
    # support 1 -- recency must rank the newer one higher.
    assert by_path["recent_pair.py"]["support"] == by_path["early_pair.py"]["support"] == 1
    assert by_path["recent_pair.py"]["score"] > by_path["early_pair.py"]["score"]
    order = [r["path"] for r in result["results"]]
    assert order.index("recent_pair.py") < order.index("early_pair.py")


def test_cochanges_skips_huge_commit_entirely(ws):
    root, shas = ws
    result = code_graph.cochanges("a.py", workspace=root, min_support=1, limit=200)
    paths = {r["path"] for r in result["results"]}
    assert not any(p.startswith("bulk/o") for p in paths)
    # the huge commit must not even be counted as a target-touching commit.
    assert result["target_commit_count"] == 9


def test_cochanges_filters_lockfiles(ws):
    root, shas = ws
    result = code_graph.cochanges("a.py", workspace=root, min_support=1, limit=200)
    paths = {r["path"] for r in result["results"]}
    assert "package-lock.json" not in paths


def test_cochanges_min_support_filters_out_singletons(ws):
    root, shas = ws
    result = code_graph.cochanges("a.py", workspace=root, min_support=2, limit=20)
    paths = {r["path"] for r in result["results"]}
    assert paths == {"a_test.py", "helper.py"}


def test_cochanges_last_seen_is_most_recent_pairing(ws):
    root, shas = ws
    result = code_graph.cochanges("a.py", workspace=root, min_support=1, limit=20)
    by_path = {r["path"]: r for r in result["results"]}
    # a_test.py co-changed at "b" and "d"; "d" is the more recent one.
    assert by_path["a_test.py"]["last_seen"]["commit"] == shas["d"]
    # helper.py co-changed at "g" and "h"; "h" is the more recent one.
    assert by_path["helper.py"]["last_seen"]["commit"] == shas["h"]


def test_cochanges_exists_now(ws):
    root, shas = ws
    result = code_graph.cochanges("a.py", workspace=root, min_support=1, limit=20)
    by_path = {r["path"]: r for r in result["results"]}
    assert by_path["a_test.py"]["exists_now"] is True
    assert by_path["helper.py"]["exists_now"] is False  # removed in "h"


def test_cochanges_output_text_is_bounded_and_labelled(ws):
    root, shas = ws
    result = code_graph.cochanges("a.py", workspace=root, min_support=1)
    assert "Often changed together" in result["output"]
    assert isinstance(result["output"], str)


def test_cochanges_root_outside_workspace_is_rejected(ws, tmp_path):
    root, shas = ws
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    result = code_graph.cochanges("a.py", workspace=str(outside))
    assert result["exit_code"] == 1
    assert "error" in result


def test_cochanges_no_path_given(ws):
    root, shas = ws
    result = code_graph.cochanges("", workspace=root)
    assert result["exit_code"] == 1


def test_cochanges_no_git_history(tmp_path):
    root = str(tmp_path / "nogit")
    os.makedirs(root)
    result = code_graph.cochanges("a.py", workspace=root)
    assert result["exit_code"] == 0
    assert result["results"] == []


# ── cache ────────────────────────────────────────────────────────────────

def test_cochanges_cache_is_reused_until_head_moves(ws, monkeypatch):
    root, shas = ws
    calls = {"n": 0}
    original = cgc._fetch_history

    def counting_fetch(root_, *, max_commits):
        calls["n"] += 1
        return original(root_, max_commits=max_commits)

    monkeypatch.setattr(cgc, "_fetch_history", counting_fetch)

    code_graph.cochanges("a.py", workspace=root, min_support=1)
    code_graph.cochanges("a.py", workspace=root, min_support=1)
    assert calls["n"] == 1  # second call served from cache, same HEAD

    _write(root, "a.py", "def a():\n    return 11\n")
    _write(root, "new_cochange.py", "w = 1\n")
    _commit(root, "new commit moves HEAD")

    result = code_graph.cochanges("a.py", workspace=root, min_support=1, limit=20)
    assert calls["n"] == 2  # cache invalidated by the new HEAD sha
    paths = {r["path"] for r in result["results"]}
    assert "new_cochange.py" in paths
    assert result["target_commit_count"] == 10


# ── impact() wiring ──────────────────────────────────────────────────────

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


@pytest.fixture()
def ce_db(tmp_path):
    from src.context_engine import store as ce_store
    ce_store.use_path(str(tmp_path / "ce.db"))
    try:
        yield
    finally:
        ce_store.use_path(None)


@pytest.fixture()
def impact_repo(tmp_path, ce_db):
    root = str(tmp_path / "impact_repo")
    os.makedirs(root)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")

    _write(root, "pkg/__init__.py", "")
    _write(root, "pkg/a.py", A_PY)
    _write(root, "pkg/b.py", B_PY)
    _write(root, "pkg/c.py", C_PY)
    _write(root, "tests/test_a.py", TEST_PY)
    _commit(root, "initial")

    # pkg/c.py historically co-changes with a config file the call graph
    # (which only sees calls/imports) can never reach.
    for i in range(3):
        _write(root, "pkg/c.py", C_PY + f"\n# rev {i}\n")
        _write(root, "config/c_settings.yaml", f"threshold: {i}\n")
        _commit(root, f"c + config rev {i}")

    cgc._CACHE.clear()
    cgc._CACHE_ORDER.clear()
    return root


@pytest.fixture()
def iws(impact_repo):
    ws_token = te._active_workspace.set(impact_repo)
    roots_token = te._active_workspace_roots.set((impact_repo,))
    try:
        yield impact_repo
    finally:
        te._active_workspace.reset(ws_token)
        te._active_workspace_roots.reset(roots_token)


def test_impact_includes_historical_cochanges_not_reached_by_call_graph(iws):
    code_graph.index(iws)
    result = code_graph.impact("c", workspace=iws, depth=3)
    assert result["exit_code"] == 0
    hist_paths = {r["path"] for r in result["historical_cochanges"]}
    assert "config/c_settings.yaml" in hist_paths
    assert "Often changed together" in result["output"]


def test_impact_historical_cochanges_excludes_already_reached_files(iws):
    code_graph.index(iws)
    result = code_graph.impact("c", workspace=iws, depth=3)
    reached_paths = {e["path"] for e in result["reached"]}
    hist_paths = {r["path"] for r in result["historical_cochanges"]}
    assert reached_paths & hist_paths == set()
    # pkg/b.py IS reached by the call graph (b calls c) -- it must never
    # show up twice, once as a caller and once as a "history" hint.
    assert "pkg/b.py" in reached_paths
    assert "pkg/b.py" not in hist_paths


def test_impact_include_history_false_omits_historical_cochanges(iws):
    code_graph.index(iws)
    result = code_graph.impact("c", workspace=iws, depth=3, include_history=False)
    assert result["historical_cochanges"] == []
    assert "Often changed together" not in result["output"]


def test_impact_diff_seeded_no_changes_still_has_historical_cochanges_key(iws):
    code_graph.index(iws)
    result = code_graph.impact("", workspace=iws, base_ref="HEAD")
    assert "historical_cochanges" in result


# ── tool wiring ──────────────────────────────────────────────────────────

def test_code_graph_cochanges_registered_everywhere():
    from src.agent_tools import TOOL_HANDLERS, TOOL_TAGS
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    from src.tool_capabilities import TOOL_CAPABILITIES
    from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS
    from src.tool_index_examples import EXAMPLES

    name = "code_graph_cochanges"
    schema_names = {s["function"]["name"] for s in FUNCTION_TOOL_SCHEMAS}
    assert name in TOOL_HANDLERS
    assert name in TOOL_TAGS
    assert name in schema_names
    assert name in TOOL_CAPABILITIES
    assert name in BUILTIN_TOOL_DESCRIPTIONS
    assert len(EXAMPLES.get(name, [])) >= 2


def test_code_graph_impact_schema_has_include_history_flag():
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    schema = next(s for s in FUNCTION_TOOL_SCHEMAS
                  if s["function"]["name"] == "code_graph_impact")
    assert "include_history" in schema["function"]["parameters"]["properties"]


def test_code_graph_cochanges_tool_executor(ws):
    from src.agent_tools.code_graph_tools import CodeGraphCochangesTool
    root, shas = ws
    ws_token = te._active_workspace.set(root)
    roots_token = te._active_workspace_roots.set((root,))
    try:
        result = asyncio.run(
            CodeGraphCochangesTool().execute('{"path": "a.py", "min_support": 1}', {}))
    finally:
        te._active_workspace.reset(ws_token)
        te._active_workspace_roots.reset(roots_token)
    assert result["exit_code"] == 0
    paths = {r["path"] for r in result["results"]}
    assert "a_test.py" in paths


def test_code_graph_cochanges_tool_executor_missing_path():
    from src.agent_tools.code_graph_tools import CodeGraphCochangesTool
    result = asyncio.run(CodeGraphCochangesTool().execute("{}", {}))
    assert result["exit_code"] == 1


def test_code_graph_impact_tool_executor_include_history_false(iws):
    from src.agent_tools.code_graph_tools import CodeGraphImpactTool
    code_graph.index(iws)
    result = asyncio.run(
        CodeGraphImpactTool().execute('{"symbol": "c", "include_history": false}', {}))
    assert result["exit_code"] == 0
    assert result["historical_cochanges"] == []
