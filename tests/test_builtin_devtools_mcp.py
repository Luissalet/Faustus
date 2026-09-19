"""Built-in DevTools MCP (chrome-devtools-mcp, src/builtin_mcp.py) — the
optional second browser server for performance traces, network/console
inspection and page audits.

Covers:
  * off by default: not in the NPX server list the model would see start,
    and `register_builtin_servers` skips connecting it;
  * enabled → registered with the documented name, and its launch args are
    derived from the same browser_* settings as builtin_browser (headless,
    isolated profile, CDP endpoint drops launch-only flags);
  * `browser_devtools_mcp` has a schema entry (agent_settings_schema parity
    is also checked generically by tests/test_agent_settings_schema.py);
  * toggling the setting starts/stops the server (restart_builtin_devtools),
    and the lazy dispatch-time staleness check picks up drift + being
    switched off.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import pytest

import src.settings as settings_mod

ROOT = Path(__file__).resolve().parent.parent
BASE = ["-y", "chrome-devtools-mcp@latest"]


def _load_builtin_mcp(monkeypatch):
    core = types.ModuleType("core")
    core.__path__ = []
    platform_compat = types.ModuleType("core.platform_compat")
    platform_compat.IS_WINDOWS = False
    platform_compat.which_tool = lambda name: None
    monkeypatch.setitem(sys.modules, "core", core)
    monkeypatch.setitem(sys.modules, "core.platform_compat", platform_compat)
    spec = importlib.util.spec_from_file_location("builtin_mcp_devtools_under_test", ROOT / "src" / "builtin_mcp.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def builtin_mcp(monkeypatch):
    monkeypatch.setenv("ODYSSEUS_BROWSER_EXECUTABLE", "/usr/bin/chromium")
    monkeypatch.delenv("ODYSSEUS_BROWSER_ISOLATED", raising=False)
    monkeypatch.delenv("ODYSSEUS_BROWSER_NO_SANDBOX", raising=False)
    monkeypatch.delenv("ODYSSEUS_BROWSER_PROFILE_DIR", raising=False)
    return _load_builtin_mcp(monkeypatch)


def _flag_value(args, flag):
    return args[args.index(flag) + 1]


# ── A. off by default ───────────────────────────────────────────────────────


def test_default_setting_is_off():
    assert settings_mod.DEFAULT_SETTINGS["browser_devtools_mcp"] is False


def test_server_entry_exists_with_expected_name_and_package(builtin_mcp):
    cfg = builtin_mcp._BUILTIN_NPX_SERVERS[builtin_mcp.DEVTOOLS_SERVER_ID]
    assert cfg["name"] == "Built-in: Browser DevTools"
    assert cfg["command"] == "npx"
    assert cfg["args"] == ["-y", "chrome-devtools-mcp@latest"]


async def _instant_sleep(*_a, **_k):
    """A real coroutine (not a call back into asyncio.sleep, which this
    patches globally — a lambda re-invoking `asyncio.sleep(0)` would recurse
    into itself instead of the original)."""
    return None


async def _drain_bg_tasks(module):
    """Await every background task `register_builtin_servers` scheduled."""
    for _ in range(50):
        pending = [t for t in module._BG_TASKS if not t.done()]
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)


async def test_register_builtin_servers_skips_devtools_when_disabled(builtin_mcp, monkeypatch):
    monkeypatch.setattr(settings_mod, "load_settings", lambda: {"browser_devtools_mcp": False})
    monkeypatch.setattr(builtin_mcp.asyncio, "sleep", _instant_sleep)
    connected = []

    async def fake_connect(mgr, server_id, **kw):
        connected.append(server_id)
        return True

    monkeypatch.setattr(builtin_mcp, "connect_builtin_npx_server", fake_connect)
    monkeypatch.setattr(builtin_mcp, "_find_npx", lambda: "/usr/bin/npx")

    class _Mgr:
        pass

    await builtin_mcp.register_builtin_servers(_Mgr())
    await _drain_bg_tasks(builtin_mcp)

    assert builtin_mcp.BROWSER_SERVER_ID in connected
    assert builtin_mcp.DEVTOOLS_SERVER_ID not in connected


async def test_register_builtin_servers_connects_devtools_when_enabled(builtin_mcp, monkeypatch):
    monkeypatch.setattr(settings_mod, "load_settings", lambda: {"browser_devtools_mcp": True})
    monkeypatch.setattr(builtin_mcp.asyncio, "sleep", _instant_sleep)
    connected = []

    async def fake_connect(mgr, server_id, **kw):
        connected.append(server_id)
        return True

    monkeypatch.setattr(builtin_mcp, "connect_builtin_npx_server", fake_connect)
    monkeypatch.setattr(builtin_mcp, "_find_npx", lambda: "/usr/bin/npx")

    class _Mgr:
        pass

    await builtin_mcp.register_builtin_servers(_Mgr())
    await _drain_bg_tasks(builtin_mcp)

    assert builtin_mcp.DEVTOOLS_SERVER_ID in connected


# ── B. settings → args ──────────────────────────────────────────────────────


def test_default_args_headless_persistent_no_cdp(builtin_mcp):
    args = builtin_mcp._devtools_mcp_args(BASE, settings={})

    assert "--headless" in args
    assert "--isolated" not in args
    assert "--executablePath" in args
    assert _flag_value(args, "--executablePath") == "/usr/bin/chromium"
    assert "--browserUrl" not in args


def test_isolated_profile_setting_adds_isolated(builtin_mcp):
    args = builtin_mcp._devtools_mcp_args(BASE, settings={"browser_profile": "isolated"})
    assert "--isolated" in args


def test_headless_off_setting(builtin_mcp):
    args = builtin_mcp._devtools_mcp_args(BASE + ["--headless"], settings={"browser_headless": False})
    assert "--headless" not in args


def test_cdp_endpoint_replaces_launch_flags(builtin_mcp):
    args = builtin_mcp._devtools_mcp_args(
        BASE + ["--headless", "--isolated"],
        settings={"browser_cdp_endpoint": "http://127.0.0.1:9222", "browser_profile": "isolated"},
    )
    assert "--browserUrl" in args
    assert _flag_value(args, "--browserUrl") == "http://127.0.0.1:9222"
    for flag in ("--headless", "--isolated", "--executablePath", "--channel"):
        assert flag not in args, flag


def test_devtools_launch_args_reads_settings_json(builtin_mcp, monkeypatch):
    monkeypatch.setattr(settings_mod, "load_settings", lambda: {"browser_profile": "isolated", "browser_headless": False})
    args = builtin_mcp.devtools_launch_args()
    assert "--isolated" in args
    assert "--headless" not in args


# ── C. schema ────────────────────────────────────────────────────────────────


def test_schema_has_devtools_field():
    from src.agent_settings_schema import schema_keys

    assert "browser_devtools_mcp" in schema_keys()


# ── D. toggle starts/stops the server ───────────────────────────────────────


async def test_restart_builtin_devtools_connects_when_enabled(builtin_mcp, monkeypatch):
    monkeypatch.setattr(settings_mod, "load_settings", lambda: {"browser_devtools_mcp": True})
    calls = []

    class _Mgr:
        async def disconnect_server(self, sid):
            calls.append(("disconnect", sid))

        async def connect_server(self, **kw):
            calls.append(("connect", kw["server_id"], kw["args"]))
            return True

        def set_connection_meta(self, sid, **meta):
            pass

        def get_all_statuses(self):
            return {}

    mgr = _Mgr()
    assert await builtin_mcp.restart_builtin_devtools(mgr) is True
    assert calls[0] == ("disconnect", "builtin_devtools")
    assert calls[1][0] == "connect" and calls[1][1] == "builtin_devtools"


async def test_restart_builtin_devtools_only_disconnects_when_disabled(builtin_mcp, monkeypatch):
    monkeypatch.setattr(settings_mod, "load_settings", lambda: {"browser_devtools_mcp": False})
    calls = []

    class _Mgr:
        async def disconnect_server(self, sid):
            calls.append(("disconnect", sid))

    mgr = _Mgr()
    assert await builtin_mcp.restart_builtin_devtools(mgr) is False
    assert calls == [("disconnect", "builtin_devtools")]


def test_launch_staleness_detects_a_settings_change_and_being_disabled(builtin_mcp, monkeypatch):
    class _Mgr:
        def __init__(self, launch_args, status="connected"):
            self.statuses = {"builtin_devtools": {"status": status, "launch_args": launch_args}}

        def get_all_statuses(self):
            return self.statuses

    monkeypatch.setattr(settings_mod, "load_settings", lambda: {"browser_devtools_mcp": True, "browser_profile": "isolated"})
    current = builtin_mcp.devtools_launch_args()
    assert builtin_mcp.devtools_launch_is_stale(_Mgr(current)) is False

    monkeypatch.setattr(settings_mod, "load_settings", lambda: {"browser_devtools_mcp": True, "browser_profile": "persistent"})
    assert builtin_mcp.devtools_launch_is_stale(_Mgr(current)) is True

    # Setting switched off while still connected → stale (restart disconnects it)
    monkeypatch.setattr(settings_mod, "load_settings", lambda: {"browser_devtools_mcp": False})
    assert builtin_mcp.devtools_launch_is_stale(_Mgr(current)) is True

    # Not connected → nothing to do
    dead = _Mgr(current, status="error")
    assert builtin_mcp.devtools_launch_is_stale(dead) is False


# ── E. settings-save hook (routes/auth_routes.py) ───────────────────────────


@pytest.mark.asyncio
async def test_saving_the_toggle_spawns_a_devtools_restart(monkeypatch):
    from types import SimpleNamespace
    import routes.auth_routes as auth_routes

    store = dict(settings_mod.DEFAULT_SETTINGS)
    monkeypatch.setattr(auth_routes, "migrate_from_settings", lambda: None)
    monkeypatch.setattr(auth_routes, "_load_settings", lambda: dict(store))
    monkeypatch.setattr(auth_routes, "_save_settings", lambda updated: (store.clear(), store.update(updated)))
    calls = []
    monkeypatch.setattr(auth_routes, "_spawn_devtools_mcp_restart", lambda: calls.append(True))

    class _Auth:
        def get_username_for_token(self, token):
            return "admin" if token == "s" else None

        def is_admin(self, username):
            return username == "admin"

    class _Req(SimpleNamespace):
        def __init__(self, body):
            super().__init__(cookies={auth_routes.SESSION_COOKIE: "s"}, _body=body)

        async def json(self):
            return self._body

    router = auth_routes.setup_auth_routes(_Auth())
    post = next(r.endpoint for r in router.routes if r.path == "/api/auth/settings" and "POST" in r.methods)

    # A save that does not touch the toggle never fires the hook.
    await post(_Req({"browser_headless": "false"}))
    assert calls == []

    # A save that does fires it exactly once.
    await post(_Req({"browser_devtools_mcp": "true"}))
    assert calls == [True]
    assert store["browser_devtools_mcp"] is True


# ── F. restart route ─────────────────────────────────────────────────────────


async def test_restart_route_restarts_with_current_settings(monkeypatch):
    import routes.mcp.mcp_routes as mcp_routes
    from src.mcp_manager import McpManager
    import src.builtin_mcp as builtin_mcp_mod

    monkeypatch.setattr(mcp_routes, "require_admin", lambda request: None)
    monkeypatch.setattr(settings_mod, "load_settings", lambda: {"browser_devtools_mcp": True, "browser_profile": "isolated"})
    calls = []

    async def fake_restart(mgr):
        calls.append(mgr)
        mgr._connections["builtin_devtools"] = {"status": "connected", "name": "Built-in: Browser DevTools", "tool_count": 12}
        return True

    monkeypatch.setattr(builtin_mcp_mod, "restart_builtin_devtools", fake_restart)
    mgr = McpManager()
    router = mcp_routes.setup_mcp_routes(mgr)
    route = next(r for r in router.routes if r.path == "/api/mcp/builtin_devtools/restart")

    body = await route.endpoint(request=object())

    assert calls == [mgr]
    assert body["connected"] is True and body["status"] == "connected" and body["tool_count"] == 12
    assert "--isolated" in body["args"]
