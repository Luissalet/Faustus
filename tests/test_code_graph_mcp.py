"""The code graph MCP server (mcp_servers/code_graph_server.py).

Same shape `test_brain_mcp.py` pins for the brain server:

* importing the module must not touch the code graph — `_ensure_init()` is
  paid only on the first real call;
* `list_tools()` must return every tool with a schema a client can validate
  against.

Unlike brain/memory, this server is not owner-scoped (a code graph has no
owner, only a workspace), so every functional test runs against a real
fixture workspace passed as `root` — no env var to set up or tear down.
"""
from __future__ import annotations

import asyncio
import os
import subprocess

import pytest

pytest.importorskip("mcp")

import mcp_servers.code_graph_server as cgs
from src.context_engine import store as ce_store

TOOL_NAMES = {
    "code_graph_search", "code_graph_trace", "code_graph_impact",
    "code_graph_architecture", "code_graph_communities", "code_graph_flows",
    "code_graph_affected_flows", "code_graph_snippet", "code_graph_change_risk",
}

ROUTES_PY = '''"""HTTP routes."""
from fastapi import APIRouter

from services.orders import place_order

router = APIRouter()


@router.post("/orders")
def create_order(payload):
    return place_order(payload)
'''

ORDERS_PY = '''"""Business logic."""
from services.db import commit_order


def place_order(payload):
    return commit_order(payload)
'''

DB_PY = '''"""DB writes."""


def commit_order(payload):
    return {"ok": True}
'''


def _write(root, rel, text):
    path = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(path) or root, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def _git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    ce_store.use_path(str(tmp_path / "ce.db"))
    monkeypatch.setattr(cgs, "_initialized", False)
    monkeypatch.setattr(cgs, "_engine", {})
    yield
    ce_store.use_path(None)


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "web/routes.py", ROUTES_PY)
    _write(root, "services/orders.py", ORDERS_PY)
    _write(root, "services/db.py", DB_PY)
    _git(str(root), "init", "-q")
    _git(str(root), "config", "user.email", "test@example.com")
    _git(str(root), "config", "user.name", "Test")
    _git(str(root), "add", "-A")
    _git(str(root), "commit", "-q", "-m", "initial")
    return str(root)


def _call(name, arguments):
    return asyncio.run(cgs.call_tool(name, arguments))


def test_importing_the_server_starts_nothing():
    """Must run before any other test in this file calls `_ensure_init()`."""
    assert cgs._initialized is False
    assert cgs._engine == {}
    assert cgs.server.name == "code_graph"


def test_list_tools_returns_every_usable_schema():
    tools = asyncio.run(cgs.list_tools())
    assert {t.name for t in tools} == TOOL_NAMES
    for tool in tools:
        assert tool.description and len(tool.description) > 40, tool.name
        schema = tool.inputSchema
        assert schema["type"] == "object", tool.name
        properties = schema.get("properties") or {}
        for field, spec in properties.items():
            assert isinstance(spec, dict) and "type" in spec, f"{tool.name}.{field}"
        for required in schema.get("required", []):
            assert required in properties, f"{tool.name}.{required}"


def test_an_unknown_tool_is_a_message_not_an_exception():
    out = _call("code_graph_nope", {})
    assert "Unknown tool" in out[0].text


def test_search_and_snippet(repo):
    from src import code_graph
    code_graph.index(repo)

    out = _call("code_graph_search", {"pattern": "place_order", "root": repo})
    assert '"exit_code": 0' in out[0].text
    assert "place_order" in out[0].text

    out = _call("code_graph_snippet", {"symbol": "place_order", "root": repo})
    assert "def place_order" in out[0].text


def test_trace_and_impact(repo):
    from src import code_graph
    code_graph.index(repo)

    out = _call("code_graph_trace",
               {"from_symbol": "create_order", "to_symbol": "commit_order", "root": repo})
    assert '"found": true' in out[0].text.lower()

    out = _call("code_graph_impact", {"symbol": "place_order", "root": repo})
    assert '"exit_code": 0' in out[0].text
    assert "affected_flows" in out[0].text


def test_architecture(repo):
    from src import code_graph
    code_graph.index(repo)

    out = _call("code_graph_architecture", {"root": repo})
    assert '"exit_code": 0' in out[0].text


def test_communities_list_and_detail(repo):
    from src import code_graph
    code_graph.index(repo)

    out = _call("code_graph_communities", {"root": repo, "level": 0})
    assert '"exit_code": 0' in out[0].text
    listing = code_graph.communities(repo, level=0)
    target = listing["communities"][0]["id"]

    out = _call("code_graph_communities", {"root": repo, "id": target})
    assert target in out[0].text


def test_flows_list_detail_and_affected(repo):
    from src import code_graph
    code_graph.index(repo)

    out = _call("code_graph_flows", {"root": repo})
    assert '"exit_code": 0' in out[0].text

    out = _call("code_graph_flows", {"root": repo, "entry": "create_order"})
    assert "create_order" in out[0].text

    out = _call("code_graph_affected_flows", {"root": repo, "symbol": "place_order"})
    assert "create_order" in out[0].text


def test_change_risk(repo):
    from src import code_graph
    code_graph.index(repo)

    out = _call("code_graph_change_risk", {"root": repo, "paths": ["services/orders.py"]})
    assert '"exit_code": 0' in out[0].text
    assert '"score"' in out[0].text


def test_builtin_server_is_registered():
    from src import builtin_mcp

    assert "code_graph" in builtin_mcp._BUILTIN_SERVERS
    script, name = builtin_mcp._BUILTIN_SERVERS["code_graph"]
    assert script == "mcp_servers/code_graph_server.py"
    assert "Code graph" in name
