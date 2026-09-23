"""Tests for `code_graph.flows` — execution flows and criticality (code
graph+).

Fixture repo: one HTTP route entry point calling through three files into a
`commit_order` (a `_SINK_RE` name) sink, a trivial one-hop flow for
comparison, a manufactured call cycle (must never hang the traversal), and a
depth-capped chain to check the cap actually bites.
"""
from __future__ import annotations

import asyncio
import json
import importlib
import os
import subprocess

import pytest

from src import code_graph
from src import tool_execution as te
from src.context_engine import store as ce_store

cg_flows = importlib.import_module("src.code_graph.flows")

ROUTES_PY = '''"""HTTP routes."""
from fastapi import APIRouter

from services.orders import place_order

router = APIRouter()


@router.post("/orders")
def create_order(payload):
    """The one entry point this fixture cares about."""
    return place_order(payload)
'''

ORDERS_PY = '''"""Business logic in the middle of the flow."""
from services.db import commit_order


def place_order(payload):
    validate_order(payload)
    return commit_order(payload)


def validate_order(payload):
    return True
'''

DB_PY = '''"""The side-effect sink at the end of the flow."""


def commit_order(payload):
    return {"ok": True}
'''

# A trivial, separate one-hop flow -- should rank BELOW the sink-touching one.
TRIVIAL_PY = '''"""A public root with one trivial hop, no sink, one file."""


def trivial_entry():
    return trivial_step()


def trivial_step():
    return 1
'''

# A manufactured cycle: a -> b -> a, `cycle_entry` forced to be an entry
# point via a route decorator (it otherwise has a caller -- `cycle_b` -- so
# the "public root with no non-test caller" heuristic would not pick it).
CYCLE_PY = '''"""A cycle: cycle_entry -> cycle_b -> cycle_entry."""
from fastapi import APIRouter

router = APIRouter()


@router.get("/cycle")
def cycle_entry():
    return cycle_b()


def cycle_b():
    return cycle_entry()
'''

# A long chain to exercise the depth cap.
CHAIN_PY = '''"""A long, depth-capped chain."""


def chain_root():
    return chain_1()


def chain_1():
    return chain_2()


def chain_2():
    return chain_3()


def chain_3():
    return chain_4()


def chain_4():
    return chain_5()


def chain_5():
    return chain_6()


def chain_6():
    return chain_7()


def chain_7():
    return chain_8()


def chain_8():
    return chain_9()


def chain_9():
    return "leaf"
'''

TEST_ORDERS_PY = '''"""Covers place_order but not commit_order directly."""
from services.orders import place_order


def test_place_order():
    assert place_order({}) is not None
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
    _write(root, "web/routes.py", ROUTES_PY)
    _write(root, "services/orders.py", ORDERS_PY)
    _write(root, "services/db.py", DB_PY)
    _write(root, "misc/trivial.py", TRIVIAL_PY)
    _write(root, "misc/cycle.py", CYCLE_PY)
    _write(root, "misc/chain.py", CHAIN_PY)
    _write(root, "tests/test_orders.py", TEST_ORDERS_PY)
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
    """Find a flow by its entry point's qualname (`entry_symbol`), which is
    stable regardless of the display `name` -- a route's `name` is now a
    human label like "POST /orders", not its function name (see
    `cg_flows._route_label`)."""
    return next(f for f in result["flows"] if needle in f["entry_symbol"])


# ── entry points and call tree ───────────────────────────────────────────

def test_route_is_an_entry_point_with_a_deterministic_call_tree(ws):
    code_graph.index(ws)
    result = code_graph.flows(ws, limit=50)
    assert result["exit_code"] == 0
    route_flow = _flow_named(result, "create_order")
    assert route_flow["entry_reason"] == "route"
    assert route_flow["name"] == "POST /orders"  # parsed from the route signature
    detail = code_graph.flow(ws, route_flow["id"])
    assert detail["exit_code"] == 0
    members = detail["flow"]["members"]
    names = [m["symbol"] for m in members]
    assert "place_order" in names
    assert "commit_order" in names
    # deterministic: same id twice.
    detail_again = code_graph.flow(ws, route_flow["id"])
    assert detail_again["flow"]["members"] == members


def test_flow_lookup_by_entry_name(ws):
    code_graph.index(ws)
    result = code_graph.flow(ws, "create_order")
    assert result["exit_code"] == 0
    assert result["flow"]["entry_symbol"] == "create_order"


# ── criticality ranking ──────────────────────────────────────────────────

def test_flow_touching_a_sink_ranks_above_a_trivial_one(ws):
    code_graph.index(ws)
    result = code_graph.flows(ws, limit=50)
    sink_flow = _flow_named(result, "create_order")
    trivial_flow = _flow_named(result, "trivial_entry")
    assert sink_flow["criticality"] > trivial_flow["criticality"]


def test_flows_are_sorted_by_kind_priority_then_criticality_by_default(ws):
    """Routes/tools outrank plain roots regardless of raw criticality (see
    `cg_flows._KIND_PRIORITY`); within the same kind, criticality still
    decides the order."""
    code_graph.index(ws)
    result = code_graph.flows(ws, limit=50)
    flows_list = result["flows"]
    kind_ranks = [cg_flows._KIND_PRIORITY.get(f["entry_reason"], 3) for f in flows_list]
    assert kind_ranks == sorted(kind_ranks)
    by_kind: dict = {}
    for f in flows_list:
        by_kind.setdefault(f["entry_reason"], []).append(f["criticality"])
    for scores in by_kind.values():
        assert scores == sorted(scores, reverse=True)
    # the two routes in this fixture must both outrank the two plain roots.
    route_positions = [i for i, f in enumerate(flows_list) if f["entry_reason"] == "route"]
    root_positions = [i for i, f in enumerate(flows_list) if f["entry_reason"] == "root"]
    assert route_positions and root_positions
    assert max(route_positions) < min(root_positions)


# ── cycle safety and depth cap ────────────────────────────────────────────

def test_a_call_cycle_never_hangs_the_traversal(ws):
    code_graph.index(ws)
    result = code_graph.flow(ws, "cycle_entry")
    assert result["exit_code"] == 0
    # cycle_entry -> cycle_b -> (cycle_entry already visited, stop)
    names = [m["symbol"] for m in result["flow"]["members"]]
    assert names.count("cycle_entry") == 0  # the entry itself is never a member
    assert "cycle_b" in names


def test_depth_cap_bounds_a_long_chain(ws, monkeypatch):
    monkeypatch.setattr(cg_flows, "_DEFAULT_DEPTH_CAP", 3)
    code_graph.index(ws)
    result = code_graph.flow(ws, "chain_root")
    assert result["exit_code"] == 0
    depths = [m["depth"] for m in result["flow"]["members"]]
    assert max(depths) <= 3
    names = [m["symbol"] for m in result["flow"]["members"]]
    assert "chain_9" not in names  # far past the cap


# ── affected_flows ────────────────────────────────────────────────────────

def test_affected_flows_by_symbol(ws):
    code_graph.index(ws)
    result = code_graph.affected_flows("place_order", workspace=ws)
    assert result["exit_code"] == 0
    assert any("create_order" in f["entry_symbol"] for f in result["flows"])


def test_affected_flows_by_symbol_not_in_any_flow(ws):
    code_graph.index(ws)
    result = code_graph.affected_flows("zzzznonexistentqqq12345", workspace=ws)
    assert result["exit_code"] == 1


def test_affected_flows_from_a_real_git_diff(ws):
    code_graph.index(ws)
    with open(os.path.join(ws, "services", "orders.py"), "a", encoding="utf-8") as handle:
        handle.write("\n\ndef extra_validate(payload):\n    return validate_order(payload)\n")
    result = code_graph.affected_flows("", workspace=ws, base_ref="HEAD")
    assert result["exit_code"] == 0
    assert result["mode"] == "diff"
    # extra_validate calls validate_order but nothing calls extra_validate,
    # so it becomes its own new "root" flow rather than changing create_order's.
    assert result["total"] >= 1


def test_affected_flows_with_no_diff_is_empty(ws):
    code_graph.index(ws)
    result = code_graph.affected_flows("", workspace=ws, base_ref="HEAD")
    assert result["exit_code"] == 0
    assert result["flows"] == []


# ── impact() gains affected_flows, and existing tests keep passing ───────

def test_impact_gains_affected_flows_key(ws):
    code_graph.index(ws)
    result = code_graph.impact("place_order", workspace=ws)
    assert result["exit_code"] == 0
    assert "affected_flows" in result
    assert any("create_order" in f["entry_symbol"] for f in result["affected_flows"])


def test_change_risk_gains_informational_flow_fields_for_diff_mode(ws):
    code_graph.index(ws)
    with open(os.path.join(ws, "services", "orders.py"), "w", encoding="utf-8") as handle:
        handle.write(ORDERS_PY.replace("return True", "return True  # touched"))
    result = code_graph.change_risk(None, workspace=ws, base_ref="HEAD")
    assert result["exit_code"] == 0
    assert "affected_flows_count" in result
    assert "max_flow_criticality" in result
    # never folded into the score's own arithmetic:
    assert abs(sum(f["weight"] * f["normalized"] for f in result["factors"].values()) * 100
              - result["score"]) < 0.05



# ── workspace confinement ────────────────────────────────────────────────

def test_root_outside_workspace_is_rejected(ws, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    result = code_graph.flows(str(outside), limit=5)
    assert result["exit_code"] == 1
    assert "error" in result


# ── tool/schema registration ─────────────────────────────────────────────

def test_tool_is_registered_everywhere():
    from src.agent_tools import TOOL_HANDLERS, TOOL_TAGS
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    from src.tool_capabilities import TOOL_CAPABILITIES
    from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS
    from src.tool_index_examples import EXAMPLES

    name = "code_graph_flows"
    schema_names = {s["function"]["name"] for s in FUNCTION_TOOL_SCHEMAS}
    assert name in TOOL_HANDLERS
    assert name in TOOL_TAGS
    assert name in schema_names
    assert name in TOOL_CAPABILITIES
    assert name in BUILTIN_TOOL_DESCRIPTIONS
    assert len(EXAMPLES.get(name, [])) >= 2


def test_flows_tool_executor_list(ws):
    from src.agent_tools.code_graph_tools import CodeGraphFlowsTool

    code_graph.index(ws)
    result = asyncio.run(CodeGraphFlowsTool().execute(json.dumps({"root": str(ws)}), {}))
    assert result["exit_code"] == 0
    assert len(result["flows"]) >= 1


def test_flows_tool_executor_detail_by_entry(ws):
    from src.agent_tools.code_graph_tools import CodeGraphFlowsTool

    code_graph.index(ws)
    result = asyncio.run(CodeGraphFlowsTool().execute(
        json.dumps({"root": str(ws), "entry": "create_order"}), {}))
    assert result["exit_code"] == 0
    assert result["flow"]["entry_symbol"] == "create_order"


def test_flows_tool_executor_affected_by_symbol(ws):
    from src.agent_tools.code_graph_tools import CodeGraphFlowsTool

    code_graph.index(ws)
    result = asyncio.run(CodeGraphFlowsTool().execute(
        json.dumps({"root": str(ws), "symbol": "place_order"}), {}))
    assert result["exit_code"] == 0
    assert any("create_order" in f["entry_symbol"] for f in result["flows"])


def test_flow_lists_the_tests_that_reach_it(ws):
    """Asked live which tests cover a critical flow, the agent had to grep
    the test folder: the flow now names them and gives the pytest command."""
    code_graph.index(ws)
    result = code_graph.flows(ws, limit=50, refresh=True)
    route_flow = _flow_named(result, "create_order")
    detail = code_graph.flow(ws, route_flow["id"])
    assert "tests/test_orders.py" in detail["flow"]["test_files"]
    assert "pytest -q tests/test_orders.py" in detail["output"]


def test_cross_language_bare_name_guesses_are_not_calls():
    from src.code_graph.communities import cross_language_guess
    assert cross_language_guess("app/services.py", "client/src/App.jsx", "static_inferred")
    assert not cross_language_guess("app/services.py", "app/db.py", "static_inferred")
    assert not cross_language_guess("app/services.py", "client/src/App.jsx", "exact")
    assert not cross_language_guess("app/x.py", "docs/y.md", "lexical")
