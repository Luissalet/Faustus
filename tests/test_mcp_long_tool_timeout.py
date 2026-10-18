"""Catalog deadlines across real MCP stdio and private HTTP; no model calls."""
import asyncio
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import Tool

from src import mcp_manager as mm, mcp_tool_cache


@pytest.mark.parametrize("hint", [True, False, 0, -1, "900", [], {}, float("nan"),
                                  float("inf"), -float("inf"), 10 ** 999, 1e300])
def test_invalid_hints_keep_legacy_budget(monkeypatch, hint):
    monkeypatch.delenv("ODYSSEUS_MCP_CALL_TIMEOUT", raising=False)
    assert mm._tool_timeout_s({"x-timeout-s": hint}) is None
    assert mm._tool_timeout_s({"input_schema": []}) is None


@pytest.mark.parametrize("hint", [615, 900, 14400])
def test_declared_seconds_without_new_global_cap(monkeypatch, hint):
    monkeypatch.delenv("ODYSSEUS_MCP_CALL_TIMEOUT", raising=False)
    assert mm._tool_timeout_s({"x-timeout-s": hint}) == hint
    assert mm._tool_timeout_s({"input_schema": {"x-timeout-s": hint}}) == hint
    monkeypatch.setenv("ODYSSEUS_MCP_CALL_TIMEOUT", "30")
    monkeypatch.setattr(mm, "MCP_CALL_TIMEOUT_S", 30)
    assert mm._tool_timeout_s({"x-timeout-s": hint}) == 30
    assert mm._tool_timeout_s({"x-timeout-s": 15}) == 15


async def test_paginated_discovery_refresh_and_cache_preserve_hint():
    manager = mm.McpManager()
    class Session:
        hint = 900
        async def list_tools(self, cursor=None):
            return SimpleNamespace(tools=[Tool(name="long_wait", inputSchema={},
                **{"x-timeout-s": self.hint})], nextCursor=None)
    session = Session()
    manager._sessions["fixture"] = session
    manager._connections["fixture"] = {"transport": "stdio"}
    tools, _ = await manager._discover_tools_paginated(session, "fixture")
    assert tools[0]["x-timeout-s"] == 900
    manager._tools["fixture"] = tools
    old_key = mcp_tool_cache.get("fixture")["version_key"]
    session.hint = 14400
    result = await manager.refresh_server_tools("fixture")
    assert result["changed"] is True
    assert result["modified"] == ["long_wait"]
    assert mcp_tool_cache.get("fixture")["version_key"] != old_key
    assert manager._tools["fixture"][0]["x-timeout-s"] == 14400
    page = await manager.list_tools_page("fixture")
    assert page["tools"][0]["x-timeout-s"] == 14400


def test_native_wait_hints_and_dynamic_workflow_contract():
    from mcp_servers import workers_server, workflows_server, voice_server
    tools = {t.name: t.model_dump() for t in workers_server.TOOLS}
    assert tools["workers_wait"]["x-timeout-s"] == 615
    assert tools["workers_wait_for"]["x-timeout-s"] == 615
    assert tools["todo_assign_agent"]["x-timeout-s"] == 693
    assert "x-timeout-s" not in tools["dispatch_workers"]  # returns job ID
    assert workflows_server._tool_from_spec({"name": "wf_test"}).model_dump()["x-timeout-s"] == 360
    assert workflows_server._tool_from_spec({"name": "wf_test", "x-timeout-s": 14400}).model_dump()["x-timeout-s"] == 14400
    assert next(t for t in voice_server.TOOLS if t.name == "voice_session").model_dump()["x-timeout-s"] == 1080


# The native Server, tools/list and call_tool run in a separate process. Only
# clocks/socket deadlines are scaled; the bridge receives real seconds and its
# original schema/handler. Its real urllib call reaches a private HTTP server.
STDIO_FIXTURE = r'''
import asyncio, os, sys, urllib.request
sys.path.insert(0, os.environ["FIXTURE_ROOT"])
from mcp_servers import workers_server as workers
scale = float(os.environ["FIXTURE_SCALE"])
native_open = urllib.request.urlopen
urllib.request.urlopen = lambda req, timeout: native_open(req, timeout=timeout * scale)
for tool in workers.TOOLS:
    if tool.name == "workers_wait":
        setattr(tool, "x-timeout-s", float(os.environ["FIXTURE_HINT"]) * scale)
native_call = workers.call_tool
@workers.server.call_tool()
async def call(name, arguments):
    task = asyncio.create_task(native_call(name, arguments))
    while not task.done():
        await asyncio.wait({task}, timeout=.025)
        if not task.done():
            await workers.server.request_context.session.send_log_message("info", {"observer_active": True})
    return await task
asyncio.run(workers.main())
'''


@contextmanager
def private_wait_server(scale):
    calls = []
    finished = threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            from urllib.parse import urlsplit, parse_qs
            seconds = float(parse_qs(urlsplit(self.path).query)["timeout"][0])
            calls.append((self.command, seconds))
            time.sleep(seconds * scale)
            raw = json.dumps({"id": "fixture-job", "status": "running"}).encode()
            try:
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            finished.set()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls, finished
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("wait_s,hint_s,explicit_cap,cancel", [
    (300, 615, False, False), (600, 615, False, False),
    (421, 900, False, False), (421, 14400, False, False),
    (300, 615, True, False), (300, 615, False, True),
])
async def test_real_mcp_http_wait(tmp_path, monkeypatch, wait_s, hint_s, explicit_cap, cancel):
    scale = .004
    monkeypatch.setattr(mm, "MCP_CALL_TIMEOUT_S", 180 * scale)
    if explicit_cap:
        monkeypatch.setenv("ODYSSEUS_MCP_CALL_TIMEOUT", "180")
    else:
        monkeypatch.delenv("ODYSSEUS_MCP_CALL_TIMEOUT", raising=False)
    script = tmp_path / "stdio_fixture.py"
    script.write_text(STDIO_FIXTURE, encoding="utf-8")
    logs = []
    async def logging(message):
        logs.append(message)
    with private_wait_server(scale) as (base, calls, finished):
        params = StdioServerParameters(command=sys.executable, args=[str(script)], env={
            "FIXTURE_ROOT": str(Path(__file__).resolve().parents[1]), "FIXTURE_SCALE": str(scale),
            "FIXTURE_HINT": str(hint_s), "FAUSTUS_URL": base, "FAUSTUS_API_TOKEN": "",
            "ODYSSEUS_DATA_DIR": str(tmp_path / "data"), "DATABASE_URL": "sqlite:///:memory:",
        })
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write, logging_callback=logging) as session:
                await session.initialize()
                manager = mm.McpManager()
                tools, _ = await manager._discover_tools_paginated(session, "fixture")
                manager._sessions["fixture"] = session
                manager._connections["fixture"] = {"transport": "stdio"}
                manager._tools["fixture"] = tools
                task = asyncio.create_task(manager.call_tool("mcp__fixture__workers_wait", {
                    "job_id": "fixture-job", "timeout_s": wait_s}))
                if cancel:
                    await asyncio.sleep(.15)
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                else:
                    result = await task
                    assert bool(result.get("exit_code")) is explicit_cap, result
                    if not explicit_cap:
                        assert "running" in result["stdout"]
                assert await asyncio.to_thread(finished.wait, 5)
                await asyncio.sleep(.1)  # drain the late real response before closing
        assert calls == [("GET", wait_s)]
        assert logs  # notifications did not prevent the explicit short cap


async def test_hint_reaches_sdk_and_missing_hint_keeps_legacy_call(monkeypatch):
    monkeypatch.delenv("ODYSSEUS_MCP_CALL_TIMEOUT", raising=False)
    seen = []
    class Session:
        async def call_tool(self, name, args, **kwargs):
            seen.append(kwargs)
            return SimpleNamespace(content=[], isError=False)
    manager = mm.McpManager()
    await manager._do_call(Session(), "fixture", {}, timeout_s=14400)
    await manager._do_call(Session(), "fixture", {})
    assert seen[0]["read_timeout_seconds"].total_seconds() == 14400
    assert seen[1] == {}


async def test_hinted_write_timeout_is_unknown_and_never_replayed(monkeypatch):
    monkeypatch.delenv("ODYSSEUS_MCP_CALL_TIMEOUT", raising=False)
    calls, reconnects = [], []
    class Session:
        async def call_tool(self, name, args, **kwargs):
            calls.append((name, kwargs))
            await asyncio.sleep(.2)
    manager = mm.McpManager()
    manager._sessions["fixture"] = Session()
    manager._connections["fixture"] = {"transport": "stdio"}
    manager._tools["fixture"] = [{"name": "write", "x-timeout-s": .03,
        "annotations": {"readOnlyHint": False}}]
    monkeypatch.setattr(manager, "is_builtin", lambda _: True)
    monkeypatch.setattr(manager, "_stdio_owner_alive", lambda _: True)
    async def reconnect(_):
        reconnects.append(True)
        return True
    monkeypatch.setattr(manager, "_reconnect_builtin", reconnect)
    result = await manager.call_tool("mcp__fixture__write", {})
    assert result["outcome_unknown"] is True
    assert len(calls) == 1 and not reconnects


async def test_hint_is_bound_before_oauth_yields(monkeypatch):
    manager = mm.McpManager()
    manager._sessions["fixture"] = object()
    manager._connections["fixture"] = {"transport": "stdio"}
    manager._tools["fixture"] = [{"name": "write", "x-timeout-s": 900}]
    async def oauth(_):
        manager._tools["fixture"][0]["x-timeout-s"] = 14400
        return None
    monkeypatch.setattr(manager, "_oauth_ensure_valid", oauth)
    result = await manager.call_tool("mcp__fixture__write", {})
    assert result["effect_not_dispatched"] is True
    assert result["error_code"] == "MCP_CALL_BINDING_CHANGED"
