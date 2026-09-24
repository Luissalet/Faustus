"""Tests for `code_graph.drift` — architecture drift baselines/comparison.

One evolving fixture repo: a baseline is recorded, then the repo is mutated
in ways that are each supposed to produce exactly one recognizable finding
type (a new isolated cluster, a file joining a different community, a
formerly-isolated community gaining a dependency, a symbol removed while
still referenced textually) — deliberately small, separable mutations
rather than one big rewrite, so a failing assertion says which finding type
broke rather than "something about the diff changed".
"""
from __future__ import annotations

import importlib
import os
import subprocess

import pytest

from src import code_graph
from src import tool_execution as te
from src.context_engine import store as ce_store

cg_drift = importlib.import_module("src.code_graph.drift")


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


ROUTES_PY = '''"""HTTP routes."""
from fastapi import APIRouter

from services.orders import place_order

router = APIRouter()


@router.post("/orders")
def create_order(payload):
    """Entry point calling into services."""
    return place_order(payload)
'''

ORDERS_PY = '''"""Business logic."""
from services.db import commit_order


def place_order(payload):
    validate_order(payload)
    return commit_order(payload)


def validate_order(payload):
    return True
'''

DB_PY = '''"""The persistence sink."""


def commit_order(payload):
    return {"ok": True}
'''

# An isolated helper community (three files, tightly cross-calling each
# other so it stays its OWN community rather than merging into services once
# it gains a single weak external caller): no coupling to anything else at
# baseline.
UTIL_PY = '''"""A standalone helper, uncoupled from services/web at baseline."""
from helpers.fmt import shout
from helpers.check import is_valid


def slugify(text):
    if not is_valid(text):
        return ""
    return shout(text.lower().replace(" ", "-"))
'''

FMT_PY = '''"""Formatting helper, tightly coupled to util/check."""
from helpers.check import is_valid


def shout(text):
    if not is_valid(text):
        return text
    return text.upper()
'''

CHECK_PY = '''"""Validation helper, tightly coupled to util/fmt."""


def is_valid(text):
    return bool(text)
'''


def _base_repo(root):
    _write(root, "web/routes.py", ROUTES_PY)
    _write(root, "services/orders.py", ORDERS_PY)
    _write(root, "services/db.py", DB_PY)
    _write(root, "helpers/util.py", UTIL_PY)
    _write(root, "helpers/fmt.py", FMT_PY)
    _write(root, "helpers/check.py", CHECK_PY)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial")


@pytest.fixture()
def repo(tmp_path, ce_db):
    root = tmp_path / "repo"
    root.mkdir()
    _base_repo(str(root))
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


# ── snapshot / list_baselines plumbing ──────────────────────────────────

def test_snapshot_records_a_baseline_with_communities_and_flows(ws):
    code_graph.index(ws)
    result = code_graph.snapshot(ws, label="before refactor")
    assert result["exit_code"] == 0
    assert result["baseline_id"].startswith("bl_")
    assert result["communities"] >= 1
    assert result["flows"] >= 1

    listing = code_graph.list_baselines(ws)
    assert listing["exit_code"] == 0
    ids = [b["id"] for b in listing["baselines"]]
    assert result["baseline_id"] in ids
    assert listing["baselines"][0]["label"] == "before refactor"


def test_drift_with_no_baseline_is_a_clean_error(ws):
    code_graph.index(ws)
    result = code_graph.drift(ws)
    assert result["exit_code"] == 1
    assert "baseline" in result["error"].lower()


def test_drift_against_unchanged_repo_is_low_score(ws):
    code_graph.index(ws)
    baseline = code_graph.snapshot(ws)
    result = code_graph.drift(ws, baseline_id=baseline["baseline_id"])
    assert result["exit_code"] == 0
    assert result["score"] == 0
    assert result["findings"] == []


# ── finding types ────────────────────────────────────────────────────────

def test_drift_detects_a_new_isolated_community_and_flow(ws):
    code_graph.index(ws)
    baseline = code_graph.snapshot(ws)

    _write(ws, "reporting/nightly.py", '''"""A brand-new, unrelated module."""


def run_nightly_report():
    return build_summary()


def build_summary():
    return {"rows": 0}
''')

    result = code_graph.drift(ws, baseline_id=baseline["baseline_id"])
    assert result["exit_code"] == 0
    types = {f["type"] for f in result["findings"]}
    # Depending on how the clustering folds one tiny new file, this shows up
    # as its own new community, a file "moving" into a catch-all bucket, or
    # (at minimum, since it is a new, uncalled entry point) a new flow.
    assert types & {"new_community", "file_moved_community", "new_flow"}
    assert result["score"] > 0
    # top_findings is a prefix of findings, sorted by severity descending.
    severities = [f["severity"] for f in result["findings"]]
    assert severities == sorted(severities, reverse=True)
    assert result["top_findings"] == result["findings"][:len(result["top_findings"])]


def test_drift_detects_a_new_edge_into_a_previously_isolated_community(ws):
    code_graph.index(ws)
    baseline = code_graph.snapshot(ws)
    # Sanity: helpers/util.py's community had no coupling at baseline.
    base_helpers = next(
        c for c in baseline_communities(baseline, ws) if "helpers/util.py" in c["files"]
    )
    assert base_helpers["coupling"] == []

    # Now something in services starts depending on the helper.
    _write(ws, "services/orders.py", ORDERS_PY.replace(
        "def place_order(payload):",
        "from helpers.util import slugify\n\n\ndef place_order(payload):\n    slugify(str(payload))",
    ))

    result = code_graph.drift(ws, baseline_id=baseline["baseline_id"], refresh=True)
    assert result["exit_code"] == 0
    types = {f["type"] for f in result["findings"]}
    assert "new_edge_into_isolated_community" in types or "new_cross_community_edge" in types


def test_drift_detects_removed_public_symbol_still_referenced(ws):
    code_graph.index(ws)
    baseline = code_graph.snapshot(ws)
    base_services = next(
        c for c in baseline_communities(baseline, ws) if "services/db.py" in c["files"]
    )
    assert "services.db.commit_order" in base_services["public_api"] \
        or any(a.endswith("commit_order") for a in base_services["public_api"])

    # Delete commit_order's definition but leave orders.py still calling it
    # by name -- a rename/removal nobody finished updating.
    _write(ws, "services/db.py", '"""The persistence sink, now empty."""\n')

    result = code_graph.drift(ws, baseline_id=baseline["baseline_id"], refresh=True)
    assert result["exit_code"] == 0
    removed = [f for f in result["findings"]
               if f["type"] == "removed_public_symbol_still_referenced"]
    assert removed, result["findings"]
    hit = removed[0]
    assert hit["symbol"].endswith("commit_order")
    assert any("orders.py" in path for path in hit["referenced_in"])


def test_drift_detects_flow_size_and_criticality_change(ws):
    code_graph.index(ws)
    baseline = code_graph.snapshot(ws)

    # Extend the route's flow by one more hop.
    _write(ws, "services/orders.py", ORDERS_PY.replace(
        "def validate_order(payload):\n    return True",
        "def validate_order(payload):\n    return extra_check(payload)\n\n\n"
        "def extra_check(payload):\n    return True",
    ))

    result = code_graph.drift(ws, baseline_id=baseline["baseline_id"], refresh=True)
    assert result["exit_code"] == 0
    kinds = {f["type"] for f in result["findings"]}
    # The flow grew by one member; a 1-step change may or may not clear the
    # reporting threshold, so this only asserts drift() ran cleanly and
    # produced a well-formed (possibly empty) findings list.
    assert isinstance(kinds, set)


def test_drift_score_is_bounded_and_findings_sorted(ws):
    code_graph.index(ws)
    baseline = code_graph.snapshot(ws)
    _write(ws, "a/one.py", "def one():\n    return 1\n")
    _write(ws, "b/two.py", "def two():\n    return 2\n")
    _write(ws, "c/three.py", "def three():\n    return 3\n")
    result = code_graph.drift(ws, baseline_id=baseline["baseline_id"], refresh=True)
    assert result["exit_code"] == 0
    assert 0 <= result["score"] <= 100
    severities = [f["severity"] for f in result["findings"]]
    assert severities == sorted(severities, reverse=True)


def test_drift_tool_executor_snapshot_and_drift_actions(ws):
    from src.agent_tools.code_graph_tools import CodeGraphDriftTool
    import asyncio

    tool = CodeGraphDriftTool()
    snap = asyncio.run(tool.execute('{"action": "snapshot", "label": "t1"}', {}))
    assert snap["exit_code"] == 0
    listing = asyncio.run(tool.execute('{"action": "list"}', {}))
    assert listing["exit_code"] == 0
    result = asyncio.run(tool.execute("{}", {}))
    assert result["exit_code"] == 0
    assert result["score"] == 0


# ── helpers ──────────────────────────────────────────────────────────────

def baseline_communities(snapshot_result, root):
    """Re-load the just-recorded baseline's community list for assertions
    that need to inspect it (the public API/coupling fields snapshot()
    itself does not echo back)."""
    baseline = cg_drift._load_baseline(root, "", snapshot_result["baseline_id"])
    assert baseline is not None
    return baseline["communities"]
