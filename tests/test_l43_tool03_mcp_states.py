"""L43 · TOOL-03 — mcp_manager.py: `degraded`/`probing` connection states and
cursor-paginated `list_tools` (docs/spec/v2/backlog.json TOOL-03).

Before this lote a server was only ever "connecting"/"connected"/"error"/
"needs_auth"/"timeout" — a server answering slowly or erroring on every third
call read exactly like a perfectly healthy one, and a reconnect in flight
read as either the stale previous status or a bare "disconnected". Nothing
here changes those five existing states: `test_mcp_manager.py` and every
other mcp_manager test keeps passing unmodified (rule 3).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, List, Optional

import pytest

from src.mcp_manager import McpManager


class _Content:
    def __init__(self, text: str):
        self.text = text


@dataclass
class _CallResult:
    content: List[Any] = field(default_factory=list)
    isError: bool = False


class FakeSession:
    """A minimal stand-in for `mcp.ClientSession`: `call_tool` for
    execute_tool's real path, `list_tools` for the pagination route."""

    def __init__(self):
        self.fail_next = 0
        self.delay_s = 0.0
        self.tool_pages: List[List[str]] = [["a", "b"], ["c"]]

    async def call_tool(self, tool_name: str, arguments: dict) -> _CallResult:
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.fail_next > 0:
            self.fail_next -= 1
            raise RuntimeError("boom")
        return _CallResult(content=[_Content("ok")], isError=False)

    async def list_tools(self, cursor: Optional[str] = None):
        idx = int(cursor) if cursor else 0
        names = self.tool_pages[idx] if idx < len(self.tool_pages) else []
        next_idx = idx + 1
        next_cursor = str(next_idx) if next_idx < len(self.tool_pages) else None
        tools = [_FakeTool(n) for n in names]
        return _ListToolsResult(tools=tools, nextCursor=next_cursor)


class _FakeTool:
    def __init__(self, name: str):
        self.name = name
        self.description = f"tool {name}"
        self.inputSchema = {"type": "object"}


@dataclass
class _ListToolsResult:
    tools: List[Any]
    nextCursor: Optional[str] = None


@pytest.fixture
def manager():
    return McpManager()


def _connect(manager: McpManager, server_id: str, session: FakeSession) -> None:
    manager._sessions[server_id] = session
    manager._connections[server_id] = {"status": "connected", "name": server_id}


# ── degraded ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_repeated_errors_mark_the_server_degraded_not_error(manager):
    session = FakeSession()
    session.fail_next = 3
    _connect(manager, "srv1", session)
    for _ in range(3):
        result = await manager.call_tool("mcp__srv1__do_thing", {})
        assert "error" in result
    status = manager.get_server_status("srv1")
    assert status["status"] == "degraded"
    assert "3 failed tool calls" in status["degraded_reason"]


@pytest.mark.asyncio
async def test_a_couple_of_errors_under_the_threshold_stay_connected(manager):
    session = FakeSession()
    session.fail_next = 2
    _connect(manager, "srv1", session)
    for _ in range(2):
        await manager.call_tool("mcp__srv1__do_thing", {})
    assert manager.get_server_status("srv1")["status"] == "connected"


@pytest.mark.asyncio
async def test_slow_calls_mark_the_server_degraded(manager):
    session = FakeSession()
    session.delay_s = 0.0  # real sleeps would make the suite slow
    _connect(manager, "srv1", session)
    # Feed the outcome history directly with 3 "slow" successes — this is
    # exactly what 3 real 9-second calls would record, without the wait.
    for _ in range(3):
        manager._record_call_outcome("srv1", True, 9.0)
    status = manager.get_server_status("srv1")
    assert status["status"] == "degraded"
    assert "averaged" in status["degraded_reason"]


@pytest.mark.asyncio
async def test_a_successful_call_after_errors_can_still_read_degraded_within_the_window(manager):
    """Degraded looks at the whole recent window, not just the latest call —
    one success right after 3 failures does not erase the failures that are
    still inside the window."""
    session = FakeSession()
    session.fail_next = 3
    _connect(manager, "srv1", session)
    for _ in range(3):
        await manager.call_tool("mcp__srv1__do_thing", {})
    await manager.call_tool("mcp__srv1__do_thing", {})  # succeeds now
    assert manager.get_server_status("srv1")["status"] == "degraded"


def test_a_dead_stdio_server_still_reports_error_not_degraded(manager, monkeypatch):
    """`error` (nothing there) outranks `degraded` (something is there, but
    struggling) — the existing dead-stdio-owner check keeps winning."""
    manager._connections["srv1"] = {"status": "connected", "name": "srv1"}
    manager._call_outcomes["srv1"] = None  # would raise if ever consulted first
    monkeypatch.setattr(manager, "_stdio_owner_alive", lambda sid: False)
    monkeypatch.setattr("src.mcp_manager.read_stderr_tail", lambda sid: "")
    status = manager.get_server_status("srv1")
    assert status["status"] == "error"


def test_disconnected_and_error_and_needs_auth_are_unaffected(manager):
    """The pre-existing states this lote must not change."""
    assert manager.get_server_status("nope") == {"status": "disconnected"}
    manager._connections["e1"] = {"status": "error", "name": "e1", "error": "boom"}
    assert manager.get_server_status("e1")["status"] == "error"
    manager._connections["a1"] = {"status": "needs_auth", "name": "a1"}
    assert manager.get_server_status("a1")["status"] == "needs_auth"


# ── probing ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_reconnect_sets_probing_before_disconnecting_the_old_session(manager, monkeypatch):
    seen = {}

    async def fake_disconnect(server_id):
        seen["status_during_disconnect"] = manager._connections.get(server_id, {}).get("status")

    async def fake_connect_server(**kwargs):
        return True

    manager._connections["memory"] = {"status": "error", "name": "memory", "error": "boom"}
    monkeypatch.setattr(manager, "disconnect_server", fake_disconnect)
    monkeypatch.setattr(manager, "connect_server", fake_connect_server)
    ok = await manager._reconnect_builtin("memory")
    assert ok is True
    assert seen["status_during_disconnect"] == "probing"


# ── paginated list_tools ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_tools_page_walks_pages_by_cursor(manager):
    session = FakeSession()
    manager._sessions["srv1"] = session
    first = await manager.list_tools_page("srv1")
    assert [t["name"] for t in first["tools"]] == ["a", "b"]
    assert first["next_cursor"] == "1"
    assert first["paginated"] is True

    second = await manager.list_tools_page("srv1", cursor=first["next_cursor"])
    assert [t["name"] for t in second["tools"]] == ["c"]
    assert second["next_cursor"] is None


@pytest.mark.asyncio
async def test_list_tools_page_on_a_server_with_a_single_page_says_not_paginated(manager):
    session = FakeSession()
    session.tool_pages = [["only-one"]]
    manager._sessions["srv1"] = session
    page = await manager.list_tools_page("srv1")
    assert page["next_cursor"] is None
    assert page["paginated"] is False


@pytest.mark.asyncio
async def test_list_tools_page_on_a_disconnected_server_is_a_clean_empty_page(manager):
    page = await manager.list_tools_page("nope")
    assert page == {"tools": [], "next_cursor": None, "paginated": False, "error": "MCP server not connected: nope"}
