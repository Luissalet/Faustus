"""A configured stdio MCP server whose stream closed is reconnected, and the call retried once.

Seen live: the Hub's stdio session closed between two agent turns; the next
`hub_agora_*` call raised anyio.ClosedResourceError from `write_stream.send`
and came back "outcome unknown" (a write the model must not repeat), and the
server stayed dead until someone reconnected it by hand. The SDK sends the
request before it waits for the reply, so a closed/broken write stream proves
nothing was sent: reconnecting and retrying once is safe even for a write.
A connection lost while WAITING keeps the honest "outcome unknown".
"""

import asyncio
import os
import sys
import textwrap
import time
from types import SimpleNamespace

import anyio
import pytest

import src.mcp_manager as mm
from src.mcp_manager import McpManager, _request_never_sent

SID = "srv-hub-test"


class _FakeSession:
    def __init__(self, label, fail=None):
        self.label = label
        self.fail = fail
        self.calls = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if self.fail is not None:
            raise self.fail
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=f"{self.label}:{name}")],
            isError=False, structuredContent=None,
        )


def _row(transport="stdio", enabled=True):
    return SimpleNamespace(id=SID, name="Hub (test)", transport=transport, command=sys.executable,
                           args="[]", env="{}", url=None, is_enabled=enabled)


def _manager(monkeypatch, first, *, row=None, replacement=None):
    mgr = McpManager()
    mgr._sessions[SID] = first
    mgr._connections[SID] = {"status": "connected", "name": "Hub (test)", "transport": "stdio"}
    mgr._tools[SID] = [{"name": "post_message"}]
    row = row if row is not None else _row()

    class _Db:
        def query(self, *_a):
            return self

        def filter(self, *_a):
            return self

        def first(self):
            return row

        def close(self):
            pass

    monkeypatch.setattr(mm, "SessionLocal", lambda: _Db())
    reconnects = []

    async def _disconnect(server_id):
        mgr._sessions.pop(server_id, None)
        mgr._connections.pop(server_id, None)

    async def _connect(srv):
        reconnects.append(srv.id)
        if replacement is None:
            mgr._connections[srv.id] = {"status": "error", "name": srv.name, "error": "boom"}
            return
        mgr._sessions[srv.id] = replacement
        mgr._connections[srv.id] = {"status": "connected", "name": srv.name, "transport": "stdio"}
        mgr._tools[srv.id] = [{"name": "post_message"}]

    monkeypatch.setattr(mgr, "disconnect_server", _disconnect)
    monkeypatch.setattr(mgr, "_connect_with_timeout", _connect)
    return mgr, reconnects


def test_never_sent_classification():
    assert _request_never_sent(anyio.ClosedResourceError())
    assert _request_never_sent(anyio.BrokenResourceError())
    if hasattr(__builtins__, "ExceptionGroup") or sys.version_info >= (3, 11):
        assert _request_never_sent(ExceptionGroup("g", [anyio.ClosedResourceError()]))  # noqa: F821
        assert not _request_never_sent(ExceptionGroup("g", [anyio.ClosedResourceError(), ValueError()]))  # noqa: F821
    assert not _request_never_sent(RuntimeError("Connection closed"))
    assert not _request_never_sent(asyncio.TimeoutError())


async def test_closed_stream_reconnects_and_retries_a_write_once(monkeypatch):
    dead = _FakeSession("old", fail=anyio.ClosedResourceError())
    fresh = _FakeSession("new")
    mgr, reconnects = _manager(monkeypatch, dead, replacement=fresh)

    res = await mgr.call_tool(f"mcp__{SID}__post_message", {"body": "hola"})

    assert res.get("exit_code") == 0, res
    assert "new:post_message" in res["stdout"]
    assert not res.get("outcome_unknown")
    assert reconnects == [SID]
    assert len(dead.calls) == 1 and fresh.calls == [("post_message", {"body": "hola"})]
    assert mgr._sessions[SID] is fresh


async def test_lost_while_waiting_stays_outcome_unknown_without_reconnect(monkeypatch):
    lost = _FakeSession("old", fail=RuntimeError("Connection closed"))
    mgr, reconnects = _manager(monkeypatch, lost, replacement=_FakeSession("new"))

    res = await mgr.call_tool(f"mcp__{SID}__post_message", {"body": "hola"})

    assert res.get("outcome_unknown") is True, res
    assert reconnects == []


async def test_failed_reconnect_is_a_plain_error_and_status_is_honest(monkeypatch):
    dead = _FakeSession("old", fail=anyio.ClosedResourceError())
    mgr, reconnects = _manager(monkeypatch, dead, replacement=None)

    res = await mgr.call_tool(f"mcp__{SID}__post_message", {})

    assert res.get("exit_code") == 1 and not res.get("outcome_unknown"), res
    assert reconnects == [SID]
    assert mgr._connections[SID]["status"] == "error"


async def test_remote_transports_are_not_restarted(monkeypatch):
    dead = _FakeSession("old", fail=anyio.ClosedResourceError())
    mgr, reconnects = _manager(monkeypatch, dead, row=_row(transport="http"),
                               replacement=_FakeSession("new"))

    res = await mgr.call_tool(f"mcp__{SID}__post_message", {})

    assert res.get("exit_code") == 1 and not res.get("outcome_unknown"), res
    assert reconnects == []


async def test_a_session_already_replaced_is_reused_not_torn_down(monkeypatch):
    dead = _FakeSession("old", fail=anyio.ClosedResourceError())
    mgr, reconnects = _manager(monkeypatch, dead, replacement=_FakeSession("unused"))
    other = _FakeSession("other")

    async def _swap_then_fail(name, arguments):
        dead.calls.append((name, arguments))
        mgr._sessions[SID] = other  # a concurrent call already reconnected
        raise anyio.ClosedResourceError()

    dead.call_tool = _swap_then_fail

    res = await mgr.call_tool(f"mcp__{SID}__post_message", {})

    assert res.get("exit_code") == 0, res
    assert "other:post_message" in res["stdout"]
    assert reconnects == []


_REAL_SERVER = textwrap.dedent(
    '''
    import asyncio, os
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import Tool, TextContent

    server = Server("fake-hub")

    @server.list_tools()
    async def _tools():
        return [Tool(name="post_message", description="writes; answers with the pid",
                     inputSchema={"type": "object", "properties": {}})]

    @server.call_tool()
    async def _call(name, arguments):
        return [TextContent(type="text", text=str(os.getpid()))]

    async def main():
        async with stdio_server() as (r, w):
            await server.run(r, w, server.create_initialization_options())

    asyncio.run(main())
    '''
)


async def test_real_stdio_server_killed_between_calls_comes_back(monkeypatch, tmp_path):
    """End to end with a real child process: kill it, the next write reconnects."""
    import psutil

    script = tmp_path / "fake_hub.py"
    script.write_text(_REAL_SERVER, encoding="utf-8")
    row = SimpleNamespace(id=SID, name="Hub (real fake)", transport="stdio", command=sys.executable,
                          args=f'["{script.as_posix()}"]', env="{}", url=None, is_enabled=True)

    class _Db:
        def query(self, *_a):
            return self

        def filter(self, *_a):
            return self

        def first(self):
            return row

        def close(self):
            pass

    monkeypatch.setattr(mm, "SessionLocal", lambda: _Db())
    mgr = McpManager()
    try:
        await mgr._connect_with_timeout(row)
        assert mgr.get_server_status(SID)["status"] == "connected"
        first = await mgr.call_tool(f"mcp__{SID}__post_message", {})
        assert first["exit_code"] == 0, first
        pid = int(first["stdout"].strip())
        psutil.Process(pid).kill()
        deadline = time.time() + 5
        while time.time() < deadline and psutil.pid_exists(pid):
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.5)  # let the transport notice the closed pipes

        second = await mgr.call_tool(f"mcp__{SID}__post_message", {})

        assert second.get("exit_code") == 0, second
        assert not second.get("outcome_unknown")
        assert int(second["stdout"].strip()) != pid
        assert mgr.get_server_status(SID)["status"] == "connected"
    finally:
        await mgr.disconnect_all()
