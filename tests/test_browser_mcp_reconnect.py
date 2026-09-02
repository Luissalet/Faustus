"""Crash-reconnect and clean shutdown of the built-in NPX (browser) server.

Audit finding 3: `_reconnect_builtin` only knew the Python built-ins, so once
the npx child died `builtin_browser` stayed dead for the whole session while
`get_server_status` still said "connected"; and `disconnect_all()` raised
"Attempted to exit cancel scope in a different task" because the server was
connected from a background task and closed from another.

The fake server below is a real stdio MCP server (the `mcp` package) that
reports its pid, so the tests can kill the child and prove a NEW process
answers afterwards. A last test drives the real `@playwright/mcp` server when
npx can start it in time, and skips otherwise.
"""

import asyncio
import os
import shutil
import signal
import sys
import textwrap
import time
from pathlib import Path

import pytest

import src.builtin_mcp as builtin_mcp
from src.mcp_manager import McpManager


ROOT = Path(__file__).resolve().parent.parent

_FAKE_SERVER = textwrap.dedent(
    '''
    import asyncio, os, sys
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import Tool, TextContent

    server = Server("fake-browser")

    @server.list_tools()
    async def _tools():
        return [
            Tool(name="browser_snapshot", description="fake snapshot", inputSchema={"type": "object", "properties": {}}),
            Tool(name="browser_pid", description="pid of this server", inputSchema={"type": "object", "properties": {}}),
        ]

    @server.call_tool()
    async def _call(name, arguments):
        if name == "browser_pid":
            return [TextContent(type="text", text=str(os.getpid()))]
        return [TextContent(type="text", text="### Page\\n- Page URL: about:blank\\n- Page Title: fake\\n### Snapshot\\n- document")]

    async def main():
        async with stdio_server() as (r, w):
            await server.run(r, w, server.create_initialization_options())

    asyncio.run(main())
    '''
)


@pytest.fixture
def fake_browser_server(tmp_path, monkeypatch):
    """Point `_BUILTIN_NPX_SERVERS['builtin_browser']` at the fake stdio server."""
    script = tmp_path / "fake_browser_server.py"
    script.write_text(_FAKE_SERVER, encoding="utf-8")
    monkeypatch.setitem(
        builtin_mcp._BUILTIN_NPX_SERVERS,
        "builtin_browser",
        {"name": "Built-in: Browser (fake)", "command": sys.executable, "args": [str(script)]},
    )
    monkeypatch.setattr(builtin_mcp, "_find_npx", lambda: sys.executable)
    # The fake is not Playwright: hand its argv through untouched, and make
    # the settings-staleness check agree so only the CRASH path reconnects.
    monkeypatch.setattr(builtin_mcp, "_npx_server_launch", lambda sid: ([str(script)], None))
    monkeypatch.setattr(builtin_mcp, "browser_launch_args", lambda settings=None: [str(script)])
    return script


async def _pid(mgr) -> int:
    res = await mgr.call_tool("mcp__builtin_browser__browser_pid", {})
    assert res.get("exit_code") == 0, res
    return int(res["stdout"].strip())


def _kill(pid: int):
    os.kill(pid, signal.SIGKILL if hasattr(signal, "SIGKILL") else signal.SIGTERM)
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.05)


async def test_npx_builtin_reconnects_after_child_is_killed(fake_browser_server):
    mgr = McpManager()
    try:
        assert await builtin_mcp.connect_builtin_npx_server(mgr, "builtin_browser") is True
        assert mgr.get_server_status("builtin_browser")["status"] == "connected"

        first = await _pid(mgr)
        _kill(first)
        # give the transport a moment to notice the closed pipe
        await asyncio.sleep(0.3)

        # Status is honest once the process is gone…
        # (the owner task may still be parked on its close event; the call
        # path below is what must recover either way)
        second = await _pid(mgr)
        assert second != first
        assert mgr.get_server_status("builtin_browser")["status"] == "connected"
        # the reconnected server answers normally
        snap = await mgr.call_tool("mcp__builtin_browser__browser_snapshot", {})
        assert snap["exit_code"] == 0 and "Page URL" in snap["stdout"]
    finally:
        await mgr.disconnect_all()


async def test_disconnect_from_another_task_is_clean(fake_browser_server, caplog):
    """Connect in one task (like `_spawn_bg` at startup), disconnect from another
    (like the app shutdown hook): no cancel-scope error, no warning."""
    mgr = McpManager()
    connected = asyncio.Event()
    release = asyncio.Event()

    async def _connector():
        await builtin_mcp.connect_builtin_npx_server(mgr, "builtin_browser")
        connected.set()
        await release.wait()

    task = asyncio.create_task(_connector())
    await asyncio.wait_for(connected.wait(), timeout=30)
    pid = await _pid(mgr)

    caplog.clear()
    await mgr.disconnect_all()  # from THIS task, not the connector's
    release.set()
    await task

    assert "builtin_browser" not in mgr._sessions
    assert mgr.get_server_status("builtin_browser") == {"status": "disconnected"}
    assert "cancel scope" not in caplog.text
    assert "Error closing MCP server" not in caplog.text
    # the child is really gone
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            break
        await asyncio.sleep(0.05)
    else:  # pragma: no cover - only on a leaked process
        pytest.fail("server child process was not terminated")


async def test_status_is_honest_when_reconnect_fails(fake_browser_server, monkeypatch):
    mgr = McpManager()
    try:
        assert await builtin_mcp.connect_builtin_npx_server(mgr, "builtin_browser") is True
        pid = await _pid(mgr)
        _kill(pid)
        await asyncio.sleep(0.3)
        # make the restart impossible
        monkeypatch.setattr(builtin_mcp, "_npx_server_launch", lambda sid: (["/nonexistent/server.py"], None))
        res = await mgr.call_tool("mcp__builtin_browser__browser_pid", {})
        assert res["exit_code"] == 1
        assert "reconnect failed" in res["error"]
        assert mgr.get_server_status("builtin_browser")["status"] == "error"
    finally:
        await mgr.disconnect_all()


@pytest.mark.skipif(not shutil.which("npx"), reason="npx not on PATH")
async def test_real_playwright_server_reconnects_after_kill(monkeypatch, tmp_path):
    """Opportunistic end-to-end check against the real @playwright/mcp server.
    Skipped when it cannot start within ~60 s (cold npx cache, no browser)."""
    import src.settings as settings_mod

    monkeypatch.setattr(settings_mod, "load_settings", lambda: {"browser_profile": "isolated"})
    monkeypatch.setenv("ODYSSEUS_BROWSER_MCP_CACHE", str(tmp_path / "cache"))
    if os.path.isdir("/opt/pw-browsers"):
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")
    mgr = McpManager()
    try:
        try:
            ok = await asyncio.wait_for(builtin_mcp.connect_builtin_npx_server(mgr, "builtin_browser"), timeout=60)
        except (asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001
            pytest.skip(f"playwright mcp did not start: {exc}")
        if not ok:
            pytest.skip(f"playwright mcp did not start: {mgr.get_server_status('builtin_browser')}")

        names = mgr.browser_tool_names()
        assert "mcp__builtin_browser__browser_navigate" in names
        # vision caps off by default → no mouse_*_xy tools
        assert "mcp__builtin_browser__browser_mouse_click_xy" not in names

        # find the npx child: the owner task's process is not exposed, so use
        # the process table (children of this test process running the package)
        try:
            import psutil  # type: ignore
        except ImportError:
            pytest.skip("psutil not available to locate the child process")
        me = psutil.Process()
        victims = [c for c in me.children(recursive=True) if any("playwright" in a for a in c.cmdline())]
        if not victims:
            pytest.skip("could not locate the playwright child process")
        for v in victims:
            try:
                v.kill()
            except psutil.Error:
                pass
        await asyncio.sleep(0.5)

        res = await asyncio.wait_for(
            mgr.call_tool("mcp__builtin_browser__browser_navigate", {"url": "about:blank"}), timeout=120
        )
        assert res.get("exit_code") == 0, res
        assert "about:blank" in res["stdout"]
        assert mgr.get_server_status("builtin_browser")["status"] == "connected"
    finally:
        await mgr.disconnect_all()
