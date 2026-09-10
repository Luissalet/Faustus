"""L63 · WEB-03 — the browser admin policy (off switch, code-execution
opt-in) and the snapshot-size budget must cover a per-session browser
connection too, not only the single shared `builtin_browser` id
(docs/spec/v2/backlog.json WEB-03).

`connect_session_browser` (src/builtin_mcp.py) registers a session's browser
MCP connection under `f"builtin_browser:<hash>:<hash>"`, a DIFFERENT id from
`BROWSER_MCP_SERVER_ID`. Before `_is_browser_connection`, `McpManager.
call_tool`'s policy gate only ever matched the literal shared id, so an
admin switching the browser off in Settings would have silently left every
session-scoped browser connection reachable.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, List

import pytest

from src import mcp_manager as mm
from src.mcp_manager import McpManager


class _Content:
    def __init__(self, text: str):
        self.text = text


@dataclass
class _CallResult:
    content: List[Any] = field(default_factory=list)
    isError: bool = False


class _FakeSession:
    async def call_tool(self, tool_name: str, arguments: dict) -> _CallResult:
        return _CallResult(content=[_Content("### Page\n- ok")], isError=False)


@pytest.fixture
def manager():
    return McpManager()


def _connect(manager, server_id):
    manager._sessions[server_id] = _FakeSession()
    manager._connections[server_id] = {"status": "connected", "name": server_id}


def test_is_browser_connection_matches_the_shared_and_session_scoped_ids():
    from src.builtin_mcp import session_browser_server_id

    assert mm._is_browser_connection("builtin_browser") is True
    assert mm._is_browser_connection(session_browser_server_id("alice", "task-1")) is True
    assert mm._is_browser_connection("some_other_server") is False
    assert mm._is_browser_connection("builtin_browserish") is False  # no ':' — not a real match


def test_switching_the_browser_off_blocks_a_session_scoped_connection_too(manager, monkeypatch):
    from src.builtin_mcp import session_browser_server_id
    server_id = session_browser_server_id("alice", "task-1")
    _connect(manager, server_id)
    monkeypatch.setattr(mm, "builtin_browser_policy_disabled", lambda: {"*"})

    result = asyncio.run(manager.call_tool(f"mcp__{server_id}__browser_snapshot", {}))
    assert result["blocked"] is True
    assert "switched off" in result["error"]


def test_a_session_scoped_call_never_restarts_the_shared_browser(manager, monkeypatch):
    from src.builtin_mcp import session_browser_server_id
    server_id = session_browser_server_id("alice", "task-1")
    _connect(manager, server_id)
    monkeypatch.setattr(mm, "builtin_browser_policy_disabled", lambda: set())

    calls = []

    async def fake_ensure_current():
        calls.append(True)
        return False

    monkeypatch.setattr(manager, "ensure_builtin_browser_current", fake_ensure_current)
    result = asyncio.run(manager.call_tool(f"mcp__{server_id}__browser_snapshot", {}))
    assert result.get("exit_code") == 0
    assert calls == []  # the shared-connection staleness check was never touched


def test_the_shared_connections_own_call_still_checks_staleness(manager, monkeypatch):
    _connect(manager, mm.BROWSER_MCP_SERVER_ID)
    monkeypatch.setattr(mm, "builtin_browser_policy_disabled", lambda: set())
    calls = []

    async def fake_ensure_current():
        calls.append(True)
        return False

    monkeypatch.setattr(manager, "ensure_builtin_browser_current", fake_ensure_current)
    asyncio.run(manager.call_tool(f"mcp__{mm.BROWSER_MCP_SERVER_ID}__browser_snapshot", {}))
    assert calls == [True]


def test_a_session_scoped_snapshot_result_is_still_truncated_to_budget(manager, monkeypatch):
    from src.builtin_mcp import session_browser_server_id
    server_id = session_browser_server_id("alice", "task-1")

    class _BigSession:
        async def call_tool(self, tool_name, arguments):
            return _CallResult(content=[_Content("### Page\n" + ("x" * 5000))], isError=False)

    manager._sessions[server_id] = _BigSession()
    manager._connections[server_id] = {"status": "connected", "name": server_id}
    monkeypatch.setattr(mm, "builtin_browser_policy_disabled", lambda: set())
    monkeypatch.setattr(mm, "_browser_snapshot_budget", lambda: 200)

    result = asyncio.run(manager.call_tool(f"mcp__{server_id}__browser_snapshot", {}))
    assert len(result["stdout"]) < 5000
    assert "truncated" in result["stdout"]
