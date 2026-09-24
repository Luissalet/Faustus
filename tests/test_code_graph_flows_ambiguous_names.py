"""Tests for the ambiguous-name-edge handling in `code_graph.flows`
(PENDIENTES 23-09 noche, item 4).

`_resolve` (src/context_engine/code_index.py) is where the decision is made:
an uncorroborated ambiguous same-language call is dropped rather than
guessed (already covered directly in tests/test_context_engine_code_index.py
alongside the new corroboration cases). What this file locks in is the
consumer side, through the real indexer and `code_graph.flows`: a
corroborated-but-ambiguous edge is still walked as a flow member, but at a
fraction of a normal member's weight in criticality -- so an otherwise
identical flow that only used unique/exact names scores strictly higher.
"""
from __future__ import annotations

import importlib
import os
import subprocess

import pytest

from src import code_graph
from src import tool_execution as te
from src.context_engine import store as ce_store

cg_flows = importlib.import_module("src.code_graph.flows")

# Two structurally identical two-hop routes into a sink
# (`commit_amb`/`commit_uniq`, both matching `_SINK_RE`). The only
# difference: `commit_amb` is a bare name shared with an unrelated,
# uncalled decoy in another file, so `_resolve` can only follow it via
# same-file corroboration (certainty `lexical`); `commit_uniq` is globally
# unique (certainty `static_inferred`).
ROUTES_AMB_PY = '''"""Route into the ambiguous-name sink."""
from fastapi import APIRouter

from services.amb_logic import step_amb

router = APIRouter()


@router.post("/ambiguous")
def entry_amb(payload):
    """Entry point for the ambiguous-name flow."""
    return step_amb(payload)
'''

AMB_LOGIC_PY = '''"""The ambiguous sink lives alongside its only caller."""


def commit_amb(payload):
    return {"ok": True}


def step_amb(payload):
    return commit_amb(payload)
'''

# A decoy sharing the sink's bare name, unrelated and never called -- this
# is what makes `commit_amb` ambiguous by name in the first place.
AMB_DECOY_PY = '''"""An unrelated function sharing the sink's name."""


def commit_amb(payload):
    return {"other": True}
'''

ROUTES_UNIQ_PY = '''"""Route into the uniquely-named sink -- the control."""
from fastapi import APIRouter

from services.uniq_logic import step_uniq

router = APIRouter()


@router.post("/unique")
def entry_uniq(payload):
    """Entry point for the control flow."""
    return step_uniq(payload)
'''

UNIQ_LOGIC_PY = '''"""The unique-name sink, structurally identical to the ambiguous one."""


def commit_uniq(payload):
    return {"ok": True}


def step_uniq(payload):
    return commit_uniq(payload)
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
    _write(root, "web/routes_amb.py", ROUTES_AMB_PY)
    _write(root, "services/amb_logic.py", AMB_LOGIC_PY)
    _write(root, "services/amb_decoy.py", AMB_DECOY_PY)
    _write(root, "web/routes_uniq.py", ROUTES_UNIQ_PY)
    _write(root, "services/uniq_logic.py", UNIQ_LOGIC_PY)
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


def _flow_named(result, needle):
    return next(f for f in result["flows"] if needle in f["entry_symbol"])


def test_corroborated_ambiguous_edge_is_still_walked(ws):
    """`step_amb -> commit_amb` is ambiguous (a decoy shares the name) but
    corroborated (same file) -- it must still appear as a flow member, not
    be silently dropped the way an uncorroborated ambiguous call is."""
    code_graph.index(ws)
    result = code_graph.flows(ws, limit=50)
    amb_flow = _flow_named(result, "entry_amb")
    detail = code_graph.flow(ws, amb_flow["id"])
    names = [m["symbol"] for m in detail["flow"]["members"]]
    assert "step_amb" in names
    assert "commit_amb" in names


def test_ambiguous_but_corroborated_flow_scores_lower_than_the_unique_control(ws):
    """Two structurally identical two-hop flows into a sink, differing only
    in whether the final call's name is ambiguous (needing corroboration,
    downgraded to `lexical`) or globally unique (`static_inferred`, full
    weight) -- the ambiguous one must score measurably lower."""
    code_graph.index(ws)
    result = code_graph.flows(ws, limit=50)
    amb_flow = _flow_named(result, "entry_amb")
    uniq_flow = _flow_named(result, "entry_uniq")
    assert amb_flow["criticality"] < uniq_flow["criticality"]


def test_ambiguous_call_certainty_is_lexical_in_the_flow_member(ws):
    code_graph.index(ws)
    result = code_graph.flows(ws, limit=50)
    amb_flow = _flow_named(result, "entry_amb")
    detail = code_graph.flow(ws, amb_flow["id"])
    commit = next(m for m in detail["flow"]["members"] if m["symbol"] == "commit_amb")
    assert commit["certainty"] == "lexical"

    uniq_flow = _flow_named(result, "entry_uniq")
    uniq_detail = code_graph.flow(ws, uniq_flow["id"])
    uniq_commit = next(m for m in uniq_detail["flow"]["members"] if m["symbol"] == "commit_uniq")
    assert uniq_commit["certainty"] == "static_inferred"
