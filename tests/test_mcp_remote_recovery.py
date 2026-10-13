"""Remote MCP transport lifetime and recovery with actual HTTP/SSE servers."""
import asyncio
import socket
import threading
import time

import pytest
import sqlalchemy
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from sqlalchemy.orm import sessionmaker

from core.database import McpServer
from core import database
from src import mcp_manager as mm, mcp_oauth
from src.mcp_manager import McpManager


class RemoteServer:
    def __init__(self, transport):
        self.transport = transport
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.epoch = 0
        self.writes = 0
        self.server = None
        self.thread = None

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/" + ("mcp" if self.transport == "http" else "sse")

    def start(self):
        self.epoch += 1
        mcp = FastMCP("remote-qa", host="127.0.0.1", port=self.port, log_level="ERROR")

        @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
        def read_epoch() -> int:
            return self.epoch

        @mcp.tool()
        def increment() -> int:
            self.writes += 1
            return self.writes

        app = mcp.streamable_http_app() if self.transport == "http" else mcp.sse_app()
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port,
                                     log_level="error", timeout_graceful_shutdown=0.2))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 5
        while not self.server.started and self.thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert self.server.started

    def stop(self):
        if self.server:
            self.server.should_exit = True
        if self.thread:
            self.thread.join(5)
            assert not self.thread.is_alive()


@pytest.fixture(params=["http", "sse"])
def remote(request, monkeypatch, tmp_path):
    server = RemoteServer(request.param)
    engine = sqlalchemy.create_engine(f"sqlite:///{tmp_path / 'remote.db'}")
    McpServer.__table__.create(bind=engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        db.add(McpServer(id="remote-qa", name="Remote QA", transport=server.transport, url=server.url, is_enabled=True))
        db.commit()
    monkeypatch.setattr(mm, "SessionLocal", Session)
    monkeypatch.setattr(database, "SessionLocal", Session)
    # Fixture servers answer immediately. Bound only this test's failed RPC
    # so a server-down case does not spend the product's normal 180 seconds.
    monkeypatch.setattr(mm, "MCP_CALL_TIMEOUT_S", 2)
    server.start()
    yield server
    server.stop()
    engine.dispose()


async def connect(manager, remote):
    assert await manager.connect_server("remote-qa", "Remote QA", remote.transport, url=remote.url)


async def test_real_remote_lifetime_closes_in_owner_task(remote, caplog):
    manager = McpManager()
    await asyncio.create_task(connect(manager, remote))
    owner = manager._owner_tasks["remote-qa"][0]
    assert not owner.done()
    try:
        result = await manager.call_tool("mcp__remote-qa__read_epoch", {})
        assert result["exit_code"] == 0
    finally:
        await asyncio.create_task(manager.disconnect_server("remote-qa"))
    assert owner.done()
    assert "remote-qa" not in manager._sessions
    assert "different task" not in caplog.text


async def test_real_remote_restart_recovers_next_read_without_app_restart(remote):
    manager = McpManager()
    await connect(manager, remote)
    oauth = manager._oauth_providers.get("remote-qa")
    try:
        assert (await manager.call_tool("mcp__remote-qa__read_epoch", {}))["exit_code"] == 0
        await asyncio.to_thread(remote.stop)
        await asyncio.to_thread(remote.start)
        result = await manager.call_tool("mcp__remote-qa__read_epoch", {})
        assert result["exit_code"] == 0, result
        assert "2" in result["stdout"]
        if remote.transport == "http":
            assert manager._oauth_providers.get("remote-qa") is oauth
        assert manager._connections["remote-qa"]["url"] == remote.url
    finally:
        await manager.disconnect_all()


async def test_remote_no_connection_then_server_returns(remote):
    manager = McpManager()
    await connect(manager, remote)
    try:
        await asyncio.to_thread(remote.stop)
        first = await manager.call_tool("mcp__remote-qa__read_epoch", {})
        assert first["exit_code"] == 1
        await asyncio.to_thread(remote.start)
        second = await manager.call_tool("mcp__remote-qa__read_epoch", {})
        assert second["exit_code"] == 0, second
        assert "2" in second["stdout"]
    finally:
        await manager.disconnect_all()


async def test_unknown_write_outcome_never_replays(remote, monkeypatch):
    manager = McpManager()
    await connect(manager, remote)
    native = manager._do_call
    async def lost_reply(session, name, args):
        if name == "increment":
            await native(session, name, args)
            raise ConnectionError("reply lost after applied write")
        return await native(session, name, args)
    monkeypatch.setattr(manager, "_do_call", lost_reply)
    try:
        result = await manager.call_tool("mcp__remote-qa__increment", {})
        assert result.get("outcome_unknown") is True
        assert remote.writes == 1
    finally:
        await manager.disconnect_all()


async def test_remote_read_failure_after_dispatch_recovers(remote, monkeypatch):
    manager = McpManager()
    await connect(manager, remote)
    native = manager._do_call
    calls = 0
    async def lost_once(session, name, args):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionError("connection lost")
        return await native(session, name, args)
    monkeypatch.setattr(manager, "_do_call", lost_once)
    try:
        result = await manager.call_tool("mcp__remote-qa__read_epoch", {})
        assert result["exit_code"] == 0, result
        assert calls == 2
    finally:
        await manager.disconnect_all()


async def test_remote_endpoint_changed_does_not_replay_old_call(remote, monkeypatch):
    manager = McpManager()
    await connect(manager, remote)
    async def lost(session, name, args):
        with mm.SessionLocal() as db:
            row = db.query(McpServer).filter(McpServer.id == "remote-qa").first()
            row.url = "http://127.0.0.1:1/different"
            db.commit()
        raise ConnectionError("old endpoint lost")
    monkeypatch.setattr(manager, "_do_call", lost)
    try:
        result = await manager.call_tool("mcp__remote-qa__read_epoch", {})
        assert result["exit_code"] == 1
        assert manager._connections["remote-qa"]["url"] == remote.url
    finally:
        await manager.disconnect_all()


async def test_existing_oauth_provider_survives_remote_recovery(remote):
    if remote.transport != "http":
        pytest.skip("Streamable HTTP owns the generic OAuth flow")
    from mcp.shared.auth import OAuthToken
    storage = mcp_oauth.DbTokenStorage("remote-qa")
    token = OAuthToken(access_token="synthetic-qa-token", token_type="Bearer")
    await storage.set_tokens(token)
    manager = McpManager()
    await connect(manager, remote)
    provider = manager._oauth_providers["remote-qa"]
    try:
        assert provider.context.is_token_valid()
        await asyncio.to_thread(remote.stop)
        await asyncio.to_thread(remote.start)
        result = await manager.call_tool("mcp__remote-qa__read_epoch", {})
        assert result["exit_code"] == 0, result
        assert manager._oauth_providers["remote-qa"] is provider
        assert (await storage.get_tokens()).access_token == "synthetic-qa-token"
    finally:
        await manager.disconnect_all()


async def test_cancel_pending_remote_initialization_retires_owner():
    from contextlib import asynccontextmanager
    entered = asyncio.Event()
    closed = asyncio.Event()
    @asynccontextmanager
    async def waiting_transport():
        try:
            entered.set()
            await asyncio.Event().wait()
            yield ()
        finally:
            closed.set()
    manager = McpManager()
    caller = asyncio.create_task(manager._connect_remote("waiting", "Waiting", "http://127.0.0.1", "http", waiting_transport))
    await entered.wait()
    owner = manager._owner_tasks["waiting"][0]
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert closed.is_set()
    assert owner.done()
    assert "waiting" not in manager._owner_tasks
    assert "waiting" not in manager._sessions
