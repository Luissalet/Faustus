"""Lote 67 — ola A wiring: `src/tool_execution.py` connects a task's own
isolated browser MCP connection (`src.builtin_mcp.connect_session_browser`,
WEB-03) the first time that task calls a `browser_*` tool, and routes the
call through it — instead of every task sharing the one `BROWSER_SERVER_ID`
connection, defeating the per-task isolation WEB-03 built. Before this lote:
`grep -rn "connect_session_browser" src/` matched only the function's own
definition in `src/builtin_mcp.py` — no caller anywhere.

The matching disconnect (`disconnect_session_browser`, at run end) is
`src/agent_loop.py::stream_agent_loop` — covered by the existing
`tests/test_l44_agent_loop_browser_session_close.py`, which still passes
unchanged with this lote's upgrade (bookkeeping-close -> full disconnect).
"""
from __future__ import annotations

import json

import pytest

import src.tool_execution as tool_execution
from src.agent_tools import ToolBlock
from src.builtin_mcp import session_browser_server_id


def _block(tool_type: str, args: dict) -> ToolBlock:
    return ToolBlock(tool_type=tool_type, content=json.dumps(args))


SNAPSHOT_WITH_REF = (
    "- Page URL: https://example.com/cart\n"
    "- Page Title: Cart\n"
    "- button \"Checkout\" [ref=e7]\n"
)


class _FakeMcp:
    """Records every call_tool invocation; answers a snapshot on
    `browser_snapshot` (any server id) and a fixed action result otherwise.
    `get_all_statuses` reflects whatever `_connected` this test primed."""

    def __init__(self, connected: "set[str]" = frozenset()):
        self.calls: list[tuple[str, dict]] = []
        self._connected = set(connected)

    async def call_tool(self, tool: str, args: dict):
        self.calls.append((tool, dict(args)))
        if tool.endswith("__browser_snapshot"):
            return {"stdout": SNAPSHOT_WITH_REF, "exit_code": 0}
        return {"stdout": "Clicked", "exit_code": 0}

    def get_all_statuses(self):
        return {sid: {"status": "connected"} for sid in self._connected}


@pytest.mark.asyncio
async def test_first_browser_call_connects_the_session_browser_and_routes_through_it(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    mcp = _FakeMcp()
    monkeypatch.setattr(tool_execution, "get_mcp_manager", lambda: mcp)

    connect_calls = []
    server_id = session_browser_server_id("alice", "task-1")

    async def fake_connect(mgr, owner_id, task_id, **kw):
        connect_calls.append((owner_id, task_id))
        mgr._connected.add(server_id)
        return True, server_id

    monkeypatch.setattr("src.builtin_mcp.connect_session_browser", fake_connect)

    desc, result = await tool_execution._execute_tool_block_impl(
        _block("mcp__builtin_browser__browser_click", {"element": "Checkout", "ref": "e7"}),
        session_id="task-1", owner="alice",
    )

    assert connect_calls == [("alice", "task-1")]
    # The precondition snapshot, the real click AND the post-action
    # readback snapshot (WEB-04) all went to the SESSION-SCOPED server id,
    # never the shared "builtin_browser" one.
    tools_called = [c[0] for c in mcp.calls]
    assert tools_called == [
        f"mcp__{server_id}__browser_snapshot",
        f"mcp__{server_id}__browser_click",
        f"mcp__{server_id}__browser_snapshot",
    ]
    assert result["stdout"] == "Clicked"


@pytest.mark.asyncio
async def test_a_second_call_reuses_the_already_connected_session_browser(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    server_id = session_browser_server_id("alice", "task-1")
    mcp = _FakeMcp(connected={server_id})
    monkeypatch.setattr(tool_execution, "get_mcp_manager", lambda: mcp)

    connect_calls = []

    async def fake_connect(mgr, owner_id, task_id, **kw):
        connect_calls.append((owner_id, task_id))
        return True, server_id
    monkeypatch.setattr("src.builtin_mcp.connect_session_browser", fake_connect)

    await tool_execution._execute_tool_block_impl(
        _block("mcp__builtin_browser__browser_click", {"element": "Checkout", "ref": "e7"}),
        session_id="task-1", owner="alice",
    )

    assert connect_calls == []  # already connected — never re-dialed
    tools_called = [c[0] for c in mcp.calls]
    assert tools_called == [
        f"mcp__{server_id}__browser_snapshot",
        f"mcp__{server_id}__browser_click",
        f"mcp__{server_id}__browser_snapshot",
    ]


@pytest.mark.asyncio
async def test_no_owner_or_session_falls_back_to_the_shared_browser(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    mcp = _FakeMcp()
    monkeypatch.setattr(tool_execution, "get_mcp_manager", lambda: mcp)

    connect_calls = []

    async def fake_connect(mgr, owner_id, task_id, **kw):
        connect_calls.append((owner_id, task_id))
        return True, "irrelevant"
    monkeypatch.setattr("src.builtin_mcp.connect_session_browser", fake_connect)

    desc, result = await tool_execution._execute_tool_block_impl(
        _block("mcp__builtin_browser__browser_click", {"element": "Checkout", "ref": "e7"}),
        session_id=None, owner=None,
    )

    assert connect_calls == []
    tools_called = [c[0] for c in mcp.calls]
    assert tools_called == [
        "mcp__builtin_browser__browser_snapshot",
        "mcp__builtin_browser__browser_click",
        "mcp__builtin_browser__browser_snapshot",
    ]
    assert result["stdout"] == "Clicked"


@pytest.mark.asyncio
async def test_a_failed_session_connect_falls_back_to_the_shared_browser_without_crashing(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    mcp = _FakeMcp()
    monkeypatch.setattr(tool_execution, "get_mcp_manager", lambda: mcp)

    async def fake_connect(mgr, owner_id, task_id, **kw):
        return False, f"builtin_browser:{owner_id}:{task_id}"
    monkeypatch.setattr("src.builtin_mcp.connect_session_browser", fake_connect)

    desc, result = await tool_execution._execute_tool_block_impl(
        _block("mcp__builtin_browser__browser_click", {"element": "Checkout", "ref": "e7"}),
        session_id="task-1", owner="alice",
    )

    tools_called = [c[0] for c in mcp.calls]
    assert tools_called == [
        "mcp__builtin_browser__browser_snapshot",
        "mcp__builtin_browser__browser_click",
        "mcp__builtin_browser__browser_snapshot",
    ]
    assert result["stdout"] == "Clicked"


@pytest.mark.asyncio
async def test_a_raising_session_connect_falls_back_without_crashing(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    mcp = _FakeMcp()
    monkeypatch.setattr(tool_execution, "get_mcp_manager", lambda: mcp)

    async def fake_connect(mgr, owner_id, task_id, **kw):
        raise RuntimeError("npx exploded")
    monkeypatch.setattr("src.builtin_mcp.connect_session_browser", fake_connect)

    desc, result = await tool_execution._execute_tool_block_impl(
        _block("mcp__builtin_browser__browser_click", {"element": "Checkout", "ref": "e7"}),
        session_id="task-1", owner="alice",
    )

    tools_called = [c[0] for c in mcp.calls]
    assert tools_called == [
        "mcp__builtin_browser__browser_snapshot",
        "mcp__builtin_browser__browser_click",
        "mcp__builtin_browser__browser_snapshot",
    ]
    assert result["stdout"] == "Clicked"


@pytest.mark.asyncio
async def test_non_browser_mcp_tools_are_unaffected(monkeypatch):
    """The session-browser wiring only ever touches is_browser_action() —
    an ordinary MCP tool call must never trigger a connect attempt."""
    monkeypatch.setenv("AUTH_ENABLED", "false")
    mcp = _FakeMcp()
    monkeypatch.setattr(tool_execution, "get_mcp_manager", lambda: mcp)

    connect_calls = []

    async def fake_connect(mgr, owner_id, task_id, **kw):
        connect_calls.append((owner_id, task_id))
        return True, "irrelevant"
    monkeypatch.setattr("src.builtin_mcp.connect_session_browser", fake_connect)

    class _OtherMcp(_FakeMcp):
        async def call_tool(self, tool, args):
            self.calls.append((tool, dict(args)))
            return {"stdout": "ok", "exit_code": 0}

    other = _OtherMcp()
    monkeypatch.setattr(tool_execution, "get_mcp_manager", lambda: other)

    await tool_execution._execute_tool_block_impl(
        _block("mcp__some_other_server__do_thing", {}),
        session_id="task-1", owner="alice",
    )
    assert connect_calls == []
