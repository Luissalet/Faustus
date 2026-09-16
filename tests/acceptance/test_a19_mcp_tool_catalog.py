"""A19 - acceptance-parity case.

Contract (docs/spec/paridad/, A19): "Connector publishes paginated tools
and changes a schema" -> "Full bounded discovery and version-aware cache
invalidation".

Exercises the REAL `src.mcp_manager.McpManager._discover_tools_paginated`
(the pagination + version-keyed cache write used by every real connect
path: stdio/SSE/HTTP) and the REAL `notifications/tools/list_changed`
handler (`_on_tools_list_changed` / `_refresh_tools_after_change`) against
the REAL `src.mcp_tool_cache` module. The only fake is the MCP server
itself (`_PagedFakeSession`) - the external process/service this case is
about, publishing 3 pages and then changing a tool's schema, exactly as
the lot's contract asks for.
"""
from __future__ import annotations

import asyncio

import mcp.types as types
import pytest

from src import mcp_manager as mcp_manager_mod, mcp_tool_cache
from src.mcp_manager import McpManager
from tests.acceptance.conftest import record_evidence

SERVER_ID = "paged-srv-1"


def _tool(name: str, description: str = "d", schema: dict | None = None) -> types.Tool:
    return types.Tool(
        name=name, description=description,
        inputSchema=schema or {"type": "object", "properties": {}},
    )


class _PagedFakeSession:
    """Publishes `tools/list` as however many pages `self.pages` holds,
    following `cursor` like a real MCP server would - the "connector" this
    case's trigger names. `serverInfo.version` is fixed per instance so a
    schema change under the SAME version is what exercises the catalog-hash
    half of the cache key, not just the version-bump half."""

    def __init__(self, pages, version="1.0.0"):
        self.pages = pages  # list of (tools, next_cursor)
        self.version = version
        self.list_tools_calls = []

    async def initialize(self):
        return types.InitializeResult(
            protocolVersion="2025-06-18",
            capabilities=types.ServerCapabilities(),
            serverInfo=types.Implementation(name="fake-connector", version=self.version),
        )

    async def list_tools(self, cursor=None):
        self.list_tools_calls.append(cursor)
        idx = 0 if cursor is None else int(cursor)
        tools, next_cursor = self.pages[idx]
        return types.ListToolsResult(tools=tools, nextCursor=next_cursor)


@pytest.fixture(autouse=True)
def _clear_cache():
    mcp_tool_cache.clear()
    yield
    mcp_tool_cache.clear()


def _three_pages():
    return [
        ([_tool("alpha"), _tool("beta")], "1"),
        ([_tool("gamma")], "2"),
        ([_tool("delta")], None),
    ]


def test_discovery_follows_all_pages_under_the_cap():
    session = _PagedFakeSession(_three_pages())

    async def go():
        manager = McpManager()
        init_result = await session.initialize()
        return await manager._discover_tools_paginated(
            session, SERVER_ID, init_result.serverInfo.version,
        )

    tools, truncated = asyncio.run(go())

    assert [t["name"] for t in tools] == ["alpha", "beta", "gamma", "delta"]
    assert truncated is False
    assert session.list_tools_calls == [None, "1", "2"]

    cached = mcp_tool_cache.get(SERVER_ID)
    assert cached is not None
    assert [t["name"] for t in cached["tools"]] == ["alpha", "beta", "gamma", "delta"]


def test_discovery_is_bounded_by_mcp_discovery_max_pages(monkeypatch):
    session = _PagedFakeSession(_three_pages())
    import src.settings as settings_mod
    monkeypatch.setattr(
        settings_mod, "load_settings",
        lambda: {**settings_mod.DEFAULT_SETTINGS, "mcp_discovery_max_pages": 2},
    )

    async def go():
        manager = McpManager()
        return await manager._discover_tools_paginated(session, SERVER_ID, "1.0.0")

    tools, truncated = asyncio.run(go())

    # Capped at 2 pages: alpha/beta (page 1) + gamma (page 2) - delta
    # (page 3) never fetched, and the caller is told it was truncated
    # rather than being handed a partial catalog silently.
    assert [t["name"] for t in tools] == ["alpha", "beta", "gamma"]
    assert truncated is True
    assert session.list_tools_calls == [None, "1"]


@pytest.mark.acceptance("A19")
def test_schema_change_invalidates_cache_and_model_gets_new_schema(monkeypatch, request):
    """A19: "Connector publishes paginated tools and changes a schema" ->
    "Full bounded discovery and version-aware cache invalidation". A fake
    connector publishes 3 pages (proving full bounded discovery), then
    changes one tool's input schema under the SAME serverInfo.version and
    fires `notifications/tools/list_changed`; the version-keyed cache
    (serverInfo.version + catalog hash) is invalidated and the manager's
    live tool catalog - what `get_all_tools`/`get_all_openai_schemas` feed
    the model - is refreshed with the new schema, not served stale."""
    session = _PagedFakeSession(_three_pages(), version="1.0.0")

    async def go():
        manager = McpManager()
        init_result = await session.initialize()
        tools, truncated = await manager._discover_tools_paginated(
            session, SERVER_ID, init_result.serverInfo.version,
        )
        manager._tools[SERVER_ID] = tools
        manager._sessions[SERVER_ID] = session
        manager._connections[SERVER_ID] = {"status": "connected", "name": "Fake Connector"}
        gen_before = manager._generation
        cache_before = mcp_tool_cache.get(SERVER_ID)

        old_schema = next(t for t in manager.get_all_tools() if t["name"] == "gamma")["input_schema"]

        # Same version, changed schema for "gamma" - the case this cache
        # exists for: a version bump is not the only way a catalog changes.
        session.pages = [
            ([_tool("alpha"), _tool("beta")], "1"),
            ([_tool("gamma", schema={"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]})], "2"),
            ([_tool("delta")], None),
        ]

        # Real notification path: the message handler McpManager wires into
        # every ClientSession via `_open_client_session`.
        handler = mcp_manager_mod._make_tools_changed_handler(manager, SERVER_ID)
        notif = types.ServerNotification(types.ToolListChangedNotification(method="notifications/tools/list_changed"))
        await handler(notif)

        # The handler schedules the refresh as a background task.
        refresh_task = manager._tools_refresh_tasks.get(SERVER_ID)
        assert refresh_task is not None
        await refresh_task

        new_schema = next(t for t in manager.get_all_tools() if t["name"] == "gamma")["input_schema"]
        cache_after = mcp_tool_cache.get(SERVER_ID)
        return (
            truncated, gen_before, manager._generation, cache_before, cache_after,
            old_schema, new_schema, session.list_tools_calls,
        )

    (truncated, gen_before, gen_after, cache_before, cache_after,
     old_schema, new_schema, calls) = asyncio.run(go())

    assert truncated is False, "all 3 pages were followed (full bounded discovery)"
    assert old_schema.get("required") is None
    assert new_schema.get("required") == ["x"], "the model now sees the new schema"
    assert cache_before["version_key"] != cache_after["version_key"], (
        "same serverInfo.version but a different catalog hash -> different cache key"
    )
    assert gen_after > gen_before, "tool-prompt cache generation bumped so the new schema is served"
    # 3 initial pages + 3 re-fetched pages after the change.
    assert len(calls) == 6

    record_evidence(
        request,
        server_id=SERVER_ID,
        cache_key_before=cache_before["version_key"],
        cache_key_after=cache_after["version_key"],
        old_schema=old_schema,
        new_schema=new_schema,
    )
