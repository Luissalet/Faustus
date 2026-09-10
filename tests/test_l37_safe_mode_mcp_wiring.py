"""OPS-05 (lote 37): `src/mcp_manager.py` tells `src/safe_mode.py` about
every connection attempt that lands on `error`/`needs_auth`, at every point
that sets that status — the one-line hook `tests/qa/test_qa_46_plugin_problematico.py`
(a prior lote) recorded as needed but could not add, because `mcp_manager.py`
was not in that lote's PROPIOS:

    from src import safe_mode
    safe_mode.record_mcp_connection(server_id, status)

Without this wiring, `safe_mode.MCP_QUARANTINE_THRESHOLD` consecutive
failures on the SAME real server never quarantine it — the mechanism in
`src/safe_mode.py` is fully implemented and tested in isolation
(`tests/test_ops_safe_mode.py`), but nothing ever fed it real outcomes. Each
test here drives one of the instrumented call sites directly (mirroring
`tests/test_mcp_manager.py`'s own patch-the-transport pattern, so no real
MCP server is needed) and proves BOTH that `_connections[server_id]` still
gets the right status (unchanged behavior) AND that `safe_mode` now hears
about it.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from src import safe_mode
from src.mcp_manager import McpManager


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    """`safe_mode` persists fail counts into the real settings store; give
    each test its own file so counts from one test never leak into another
    (see `tests/test_ops_safe_mode.py`'s own fixture of the same shape)."""
    import src.settings as settings_module

    monkeypatch.setattr(settings_module, "SETTINGS_FILE", str(tmp_path / "settings.json"))
    settings_module._invalidate_caches()
    yield


def test_connect_server_outer_exception_records_error_with_safe_mode():
    """The outer `except Exception` in `connect_server` (every transport's
    last line of defense) is exactly what `test_generic_mcp_connection_error_preserves_original_error`
    above already covers for message formatting; this pins that it ALSO
    reports to safe_mode."""
    mgr = McpManager()

    async def _boom(*a, **k):
        raise RuntimeError("child process could not start")

    with patch.object(McpManager, "_connect_stdio", side_effect=_boom):
        result = asyncio.run(mgr.connect_server("flaky-1", "Flaky", "stdio", command="nope"))

    assert result is False
    assert mgr._connections["flaky-1"]["status"] == "error"
    assert safe_mode._mcp_fail_counts().get("flaky-1") == 1, "connect_server's own failure must already have counted once"


def test_repeated_connect_server_failures_quarantine_the_real_server_id():
    """The actual point of the wiring: `MCP_QUARANTINE_THRESHOLD` consecutive
    real failures through `connect_server` — not a direct `safe_mode` call —
    quarantine the server, exactly like `tests/qa/test_qa_46_plugin_problematico.py`
    proves for a direct call."""
    mgr = McpManager()

    async def _boom(*a, **k):
        raise RuntimeError("crashes on startup")

    with patch.object(McpManager, "_connect_stdio", side_effect=_boom):
        for _ in range(safe_mode.MCP_QUARANTINE_THRESHOLD):
            asyncio.run(mgr.connect_server("problem-plugin", "Problem", "stdio", command="nope"))

    assert "problem-plugin" in safe_mode.quarantined_servers()
    from src.settings import get_setting
    assert "problem-plugin" in (get_setting("disabled_tools", []) or [])


def test_start_http_connect_timeout_marks_needs_auth_and_records_it():
    """Still awaiting authorization when the bounded wait elapses (no
    `_on_redirect` call yet) — the `needs_auth` branch at the end of
    `_start_http_connect`."""
    mgr = McpManager()

    async def _never_finishes(*a, **k):
        await asyncio.sleep(10)
        return True

    with patch.object(McpManager, "_connect_http", side_effect=_never_finishes):
        result = asyncio.run(mgr._start_http_connect("oauth-1", "OAuth Server", "https://x/mcp", wait=0.05))

    assert result is False
    assert mgr._connections["oauth-1"]["status"] == "needs_auth"
    assert safe_mode._mcp_fail_counts().get("oauth-1") == 1


def test_start_http_connect_task_exception_records_error():
    """The background connect task finishes WITHIN the bounded wait, but
    with an exception — the `except Exception` branch right after
    `task.result()`, distinct from the needs_auth/timeout path above."""
    mgr = McpManager()

    async def _fails_fast(*a, **k):
        raise RuntimeError("DNS resolution failed")

    with patch.object(McpManager, "_connect_http", side_effect=_fails_fast):
        result = asyncio.run(mgr._start_http_connect("oauth-2", "OAuth Server", "https://x/mcp", wait=1.0))

    assert result is False
    assert mgr._connections["oauth-2"]["status"] == "error"
    assert safe_mode._mcp_fail_counts().get("oauth-2") == 1


def test_needs_auth_already_published_by_on_redirect_is_not_double_counted():
    """`_start_http_connect`'s own guard (`if cur.get("status") != "needs_auth"`)
    exists so `_on_redirect` publishing needs_auth first is not overwritten —
    this pins that the safe_mode call right below that guard inherits the
    SAME protection, so one real OAuth attempt is recorded once, not twice
    (once from `_on_redirect`'s own call, simulated here, and again from the
    timeout branch it guards)."""
    mgr = McpManager()

    async def _redirects_then_hangs(*a, **k):
        # Mirrors what the real `_connect_http`'s `_on_redirect` closure does
        # the moment the auth URL is known: publish needs_auth AND tell
        # safe_mode, well before `_start_http_connect`'s bounded wait elapses.
        mgr._connections["oauth-3"] = {"status": "needs_auth", "name": "OAuth", "transport": "http", "auth_url": "https://auth"}
        safe_mode.record_mcp_connection("oauth-3", "needs_auth")
        await asyncio.sleep(10)
        return True

    with patch.object(McpManager, "_connect_http", side_effect=_redirects_then_hangs):
        asyncio.run(mgr._start_http_connect("oauth-3", "OAuth", "https://x/mcp", wait=0.05))

    assert safe_mode._mcp_fail_counts().get("oauth-3") == 1, "one real attempt must count once, not twice, when both paths could fire"


def test_reconnect_builtin_failure_records_error():
    """`_reconnect_builtin`'s own `error` branch (server process exited and
    could not be restarted) — a real reconnect attempt, not the initial
    connect. `_BUILTIN_SERVERS`/`connect_builtin_npx_server` are imported
    from `src.builtin_mcp` INSIDE the method, so the patch targets that
    module, not `src.mcp_manager`."""
    mgr = McpManager()
    mgr._connections["builtin_memory"] = {"status": "connected", "name": "Memory"}

    async def _connect_fails(*a, **k):
        return False

    with patch("src.builtin_mcp._BUILTIN_SERVERS", {"builtin_memory": ("scripts/fake.py", "Memory")}), \
         patch("src.builtin_mcp._BUILTIN_NPX_SERVERS", {}), \
         patch.object(McpManager, "connect_server", side_effect=_connect_fails):
        asyncio.run(mgr._reconnect_builtin("builtin_memory"))

    assert mgr._connections["builtin_memory"]["status"] == "error"
    assert safe_mode._mcp_fail_counts().get("builtin_memory") == 1
