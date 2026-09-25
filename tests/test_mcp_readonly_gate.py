"""Read-only local (stdio, no-network) MCP tool carve-out.

`McpManager.readonly_local_tool_allowed` (src/mcp_manager.py) lets a tool on
a LOCAL stdio MCP server pass the post-external-context approval gate
(`ToolRunSecurityContext.decision_for` / `capabilities_for_action`, both in
src/tool_capabilities.py) exactly like `read_file` always has -- but ONLY
when every one of these holds:

  1. the tool's OWN annotations say readOnlyHint=True and destructiveHint is
     not True (never the tool-name heuristic `mcp_tool_is_readonly` falls
     back to elsewhere in this codebase);
  2. the server's transport is stdio (a local child process);
  3. the server's saved declared_permissions explicitly has network: false
     (missing/unknown declared_permissions keeps the gate up).

These tests use a real `McpManager` instance with its in-memory
`_connections`/`_tools` populated by hand (no live subprocess, no network,
no DB) and monkeypatch `server_network_declared_false` to control the
declared-permissions answer, so the whole rule is exercised without I/O.
"""

import json
from types import SimpleNamespace

import pytest

import src.mcp_manager as mcp_manager_module
from src.mcp_manager import McpManager, _server_declared_network_explicitly_false
from src.tool_capabilities import ToolRunSecurityContext

SERVER_ID = "notes_server"
TOOL_NAME = "list_projects"
QUALIFIED = f"mcp__{SERVER_ID}__{TOOL_NAME}"
WRITE_TOOL_NAME = "delete_project"
WRITE_QUALIFIED = f"mcp__{SERVER_ID}__{WRITE_TOOL_NAME}"

READONLY_ANN = {"readOnlyHint": True, "destructiveHint": False}


def _manager(transport="stdio", annotations=None, connected=True, extra_tools=None):
    """A real McpManager with its in-memory registry filled in by hand."""
    mgr = McpManager()
    if connected:
        mgr._connections[SERVER_ID] = {
            "status": "connected", "name": SERVER_ID, "transport": transport,
        }
        tools = [
            {"name": TOOL_NAME, "description": "", "input_schema": {}, "annotations": annotations}
        ]
        if extra_tools:
            tools.extend(extra_tools)
        mgr._tools[SERVER_ID] = tools
    return mgr


@pytest.fixture(autouse=True)
def ask_mode(monkeypatch):
    import src.tool_capabilities as caps
    monkeypatch.setattr(caps, "tool_approval_mode", lambda: "ask")


def _armed_context() -> ToolRunSecurityContext:
    ctx = ToolRunSecurityContext()
    ctx.external_untrusted_context_seen = True
    return ctx


def _decide(monkeypatch, manager, network_false, tool_name=QUALIFIED):
    monkeypatch.setattr("src.tool_utils.get_mcp_manager", lambda: manager)
    monkeypatch.setattr(mcp_manager_module, "server_network_declared_false", lambda sid: network_false)
    return _armed_context().decision_for(tool_name, {})


# ── the integration table (through decision_for) ────────────────────────────

def test_local_stdio_readonly_no_network_allowed(monkeypatch):
    mgr = _manager(annotations=READONLY_ANN)
    decision = _decide(monkeypatch, mgr, network_false=True)
    assert decision.allowed


def test_missing_readonly_hint_name_heuristic_not_used(monkeypatch):
    # The tool is named "list_projects" -- the heuristic elsewhere in this
    # codebase would call that read-only -- but there is no annotation at
    # all, so the carve-out must NOT fire; the gate stays up.
    mgr = _manager(annotations=None)
    decision = _decide(monkeypatch, mgr, network_false=True)
    assert not decision.allowed


def test_destructive_hint_true_gated(monkeypatch):
    mgr = _manager(annotations={"readOnlyHint": True, "destructiveHint": True})
    decision = _decide(monkeypatch, mgr, network_false=True)
    assert not decision.allowed


def test_read_only_hint_false_gated(monkeypatch):
    mgr = _manager(annotations={"readOnlyHint": False})
    decision = _decide(monkeypatch, mgr, network_false=True)
    assert not decision.allowed


def test_sse_transport_gated(monkeypatch):
    mgr = _manager(transport="sse", annotations=READONLY_ANN)
    decision = _decide(monkeypatch, mgr, network_false=True)
    assert not decision.allowed


def test_http_transport_gated(monkeypatch):
    mgr = _manager(transport="http", annotations=READONLY_ANN)
    decision = _decide(monkeypatch, mgr, network_false=True)
    assert not decision.allowed


@pytest.mark.parametrize("network_false", [False], ids=["network_true_or_missing"])
def test_network_not_declared_false_gated(monkeypatch, network_false):
    mgr = _manager(annotations=READONLY_ANN)
    decision = _decide(monkeypatch, mgr, network_false=network_false)
    assert not decision.allowed


def test_unknown_server_gated(monkeypatch):
    mgr = _manager(connected=False)
    decision = _decide(monkeypatch, mgr, network_false=True)
    assert not decision.allowed


def test_write_tool_of_same_readonly_server_gated(monkeypatch):
    mgr = _manager(
        annotations=READONLY_ANN,
        extra_tools=[{"name": WRITE_TOOL_NAME, "description": "", "input_schema": {}, "annotations": None}],
    )
    # The read tool on this exact server is allowed...
    assert _decide(monkeypatch, mgr, network_false=True, tool_name=QUALIFIED).allowed
    # ...but its write sibling, with no read-only annotation, stays gated.
    decision = _decide(monkeypatch, mgr, network_false=True, tool_name=WRITE_QUALIFIED)
    assert not decision.allowed


def test_no_manager_available_gated(monkeypatch):
    monkeypatch.setattr("src.tool_utils.get_mcp_manager", lambda: None)
    decision = _armed_context().decision_for(QUALIFIED, {})
    assert not decision.allowed


def test_malformed_qualified_name_gated(monkeypatch):
    mgr = _manager(annotations=READONLY_ANN)
    monkeypatch.setattr("src.tool_utils.get_mcp_manager", lambda: mgr)
    monkeypatch.setattr(mcp_manager_module, "server_network_declared_false", lambda sid: True)
    decision = _armed_context().decision_for(f"mcp__{SERVER_ID}", {})
    assert not decision.allowed


# ── McpManager.readonly_local_tool_allowed directly ─────────────────────────

def test_manager_method_true_case(monkeypatch):
    mgr = _manager(annotations=READONLY_ANN)
    monkeypatch.setattr(mcp_manager_module, "server_network_declared_false", lambda sid: True)
    assert mgr.readonly_local_tool_allowed(QUALIFIED) is True


def test_manager_method_never_raises_on_garbage(monkeypatch):
    mgr = McpManager()
    assert mgr.readonly_local_tool_allowed(None) is False
    assert mgr.readonly_local_tool_allowed("") is False
    assert mgr.readonly_local_tool_allowed("not_mcp_shaped") is False
    assert mgr.readonly_local_tool_allowed("mcp__only_two_parts") is False


# ── raw declared_permissions parsing: explicit false vs merely absent ───────

def test_declared_network_explicitly_false():
    srv = SimpleNamespace(declared_permissions=json.dumps({"network": False, "files": True}))
    assert _server_declared_network_explicitly_false(srv) is True


def test_declared_network_missing_key_is_not_explicitly_false():
    # SEC: server_declared_permissions() (the coercing helper) would read
    # this the same as network=False via bool(None) -- the strict raw
    # reader used by the gate carve-out must NOT make that mistake.
    srv = SimpleNamespace(declared_permissions=json.dumps({"files": True, "secrets": True}))
    assert _server_declared_network_explicitly_false(srv) is False


def test_declared_network_true_is_not_explicitly_false():
    srv = SimpleNamespace(declared_permissions=json.dumps({"network": True}))
    assert _server_declared_network_explicitly_false(srv) is False


def test_declared_permissions_absent_entirely():
    srv = SimpleNamespace(declared_permissions=None)
    assert _server_declared_network_explicitly_false(srv) is False


def test_declared_permissions_malformed_json():
    srv = SimpleNamespace(declared_permissions="{not json")
    assert _server_declared_network_explicitly_false(srv) is False


def test_declared_permissions_not_an_object():
    srv = SimpleNamespace(declared_permissions=json.dumps([1, 2, 3]))
    assert _server_declared_network_explicitly_false(srv) is False
