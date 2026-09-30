"""Forcing one connected server to re-read its tool list.

A server can grow tools without announcing it (a new release of a bridge, a
writer app gaining a tool). `McpManager.refresh_server_tools` re-runs bounded
discovery on the live session and swaps the catalog, the version-keyed cache
and the generation the prompt cache and tool index follow -- only when the
list really changed, and never for a failed read.
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import mcp.types as types
import pytest

from src import mcp_tool_cache
from src.mcp_manager import McpManager

SID = "app-1"


def _tool(name, description="d", schema=None):
    return types.Tool(name=name, description=description, inputSchema=schema or {"type": "object", "properties": {}})


class FakeSession:
    def __init__(self, tools, version="1.0.0"):
        self.tools = tools
        self.version = version
        self.fail = None
        self.calls = 0

    async def list_tools(self, cursor=None):
        self.calls += 1
        if self.fail:
            raise self.fail
        return types.ListToolsResult(tools=list(self.tools), nextCursor=None)


@pytest.fixture(autouse=True)
def _clean_cache():
    mcp_tool_cache.clear()
    yield
    mcp_tool_cache.clear()


async def _connected(session):
    mgr = McpManager()
    mgr._sessions[SID] = session
    mgr._connections[SID] = {"status": "connected", "name": "App", "tool_count": 0}
    tools, _ = await mgr._discover_tools_paginated(session, SID, session.version)
    mgr._tools[SID] = tools
    mgr._connections[SID]["tool_count"] = len(tools)
    return mgr


def test_a_tool_added_under_the_same_version_is_picked_up():
    session = FakeSession([_tool("wh_read"), _tool("wh_write")])

    async def go():
        mgr = await _connected(session)
        key_before = mcp_tool_cache.get(SID)["version_key"]
        gen_before = mgr._generation
        session.tools.append(_tool("wh_bibliography"))
        result = await mgr.refresh_server_tools(SID)
        return mgr, key_before, gen_before, result

    mgr, key_before, gen_before, result = asyncio.run(go())
    assert result["ok"] and result["changed"]
    assert result["added"] == ["wh_bibliography"] and result["removed"] == [] and result["modified"] == []
    assert (result["previous_count"], result["tool_count"]) == (2, 3)
    assert {t["name"] for t in mgr.get_all_tools()} >= {"wh_bibliography"}
    assert mgr._connections[SID]["tool_count"] == 3
    assert mgr._generation == gen_before + 1, "the prompt cache and the tool index follow the generation"
    assert mcp_tool_cache.get(SID)["version_key"] != key_before
    assert mcp_tool_cache.get(SID)["version_key"].startswith("1.0.0:"), "the cache keeps the server's own version"


def test_removed_and_modified_tools_are_reported():
    session = FakeSession([_tool("a"), _tool("b"), _tool("c")])

    async def go():
        mgr = await _connected(session)
        session.tools = [_tool("a"), _tool("c", description="changed")]
        return await mgr.refresh_server_tools(SID)

    result = asyncio.run(go())
    assert result["changed"] and result["removed"] == ["b"] and result["modified"] == ["c"] and result["added"] == []


def test_an_unchanged_list_does_not_move_the_generation():
    session = FakeSession([_tool("a"), _tool("b")])

    async def go():
        mgr = await _connected(session)
        gen = mgr._generation
        return gen, mgr._generation, await mgr.refresh_server_tools(SID), mgr

    gen, _, result, mgr = asyncio.run(go())
    assert result["ok"] and result["changed"] is False
    assert result["added"] == result["removed"] == result["modified"] == []
    assert mgr._generation == gen


def test_a_failed_read_keeps_the_old_catalog_and_cache():
    session = FakeSession([_tool("a"), _tool("b")])

    async def go():
        mgr = await _connected(session)
        cache_before = dict(mcp_tool_cache.get(SID))
        gen = mgr._generation
        session.fail = RuntimeError("bridge died")
        result = await mgr.refresh_server_tools(SID)
        return mgr, cache_before, gen, result

    mgr, cache_before, gen, result = asyncio.run(go())
    assert result["ok"] is False and "bridge died" in result["error"]
    assert [t["name"] for t in mgr._tools[SID]] == ["a", "b"]
    assert mcp_tool_cache.get(SID) == cache_before
    assert mgr._generation == gen


def test_a_server_that_is_not_connected_is_reported_not_raised():
    result = asyncio.run(McpManager().refresh_server_tools("nobody"))
    assert result["ok"] is False and result["error"] == "not connected"


def test_a_session_replaced_during_the_read_does_not_get_the_old_catalog():
    session = FakeSession([_tool("a")])

    async def go():
        mgr = await _connected(session)
        original = session.list_tools

        async def slow(cursor=None):
            mgr._sessions[SID] = FakeSession([_tool("z")])  # reconnected meanwhile
            return await original(cursor)

        session.list_tools = slow
        return mgr, await mgr.refresh_server_tools(SID)

    mgr, result = asyncio.run(go())
    assert result["ok"] is False
    assert [t["name"] for t in mgr._tools[SID]] == ["a"]


def test_a_reconnect_with_a_different_list_replaces_the_cache_entry():
    """Every connect path ends in `_discover_tools_paginated`, which overwrites
    the cache with the new version/hash key: a reconnect can never be served the
    list the previous process had."""
    old = FakeSession([_tool("a")])
    new = FakeSession([_tool("a"), _tool("wh_bibliography")])

    async def go():
        mgr = McpManager()
        await mgr._discover_tools_paginated(old, SID, "1.0.0")
        key_old = mcp_tool_cache.get(SID)["version_key"]
        await mgr.disconnect_server(SID)
        await mgr._discover_tools_paginated(new, SID, "1.0.0")
        return key_old, mcp_tool_cache.get(SID)

    key_old, entry = asyncio.run(go())
    assert entry["version_key"] != key_old
    assert mcp_tool_cache.is_current(SID, key_old) is False
    assert [t["name"] for t in entry["tools"]] == ["a", "wh_bibliography"]


def test_disconnect_forgets_the_remembered_server_version():
    async def go():
        mgr = McpManager()
        await mgr._discover_tools_paginated(FakeSession([_tool("a")]), SID, "2.0.0")
        assert mgr._server_versions[SID] == "2.0.0"
        await mgr.disconnect_server(SID)
        return mgr._server_versions

    assert SID not in asyncio.run(go())


# -- the agent tool -------------------------------------------------------

def _run_action(mgr, **args):
    from src.agent_tools.admin_tools import do_manage_mcp
    with patch("src.agent_tools.admin_tools.get_mcp_manager", return_value=mgr):
        return asyncio.run(do_manage_mcp(json.dumps({"action": "refresh_tools", **args})))


def test_manage_mcp_refresh_tools_says_what_changed():
    mgr = MagicMock()
    mgr.refresh_server_tools = AsyncMock(return_value={
        "ok": True, "changed": True, "previous_count": 140, "tool_count": 141,
        "added": ["wh_bibliography"], "removed": [], "modified": [],
    })
    result = _run_action(mgr, server_id="writer")
    assert result["exit_code"] == 0
    assert "140 -> 141" in result["response"] and "wh_bibliography" in result["response"]
    mgr.refresh_server_tools.assert_awaited_once_with("writer")


def test_manage_mcp_refresh_tools_unchanged_points_at_reconnect():
    mgr = MagicMock()
    mgr.refresh_server_tools = AsyncMock(return_value={
        "ok": True, "changed": False, "previous_count": 5, "tool_count": 5,
        "added": [], "removed": [], "modified": [],
    })
    result = _run_action(mgr, server_id="writer")
    assert result["exit_code"] == 0 and "unchanged" in result["response"] and "reconnect" in result["response"]


def test_manage_mcp_refresh_tools_failures_are_errors():
    mgr = MagicMock()
    mgr.refresh_server_tools = AsyncMock(return_value={"ok": False, "error": "not connected", "changed": False})
    assert _run_action(mgr, server_id="writer")["exit_code"] == 1
    assert _run_action(mgr)["exit_code"] == 1, "server_id is required"
    with patch("src.agent_tools.admin_tools.get_mcp_manager", return_value=None):
        from src.agent_tools.admin_tools import do_manage_mcp
        out = asyncio.run(do_manage_mcp(json.dumps({"action": "refresh_tools", "server_id": "x"})))
    assert out["exit_code"] == 1


def test_the_tool_schema_offers_the_action():
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS as TOOL_SCHEMAS
    spec = next(t for t in TOOL_SCHEMAS if t["function"]["name"] == "manage_mcp")
    assert "refresh_tools" in spec["function"]["parameters"]["properties"]["action"]["enum"]
