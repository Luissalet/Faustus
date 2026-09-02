"""Built-in browser (Playwright MCP) settings → launch args, snapshot budget
and the browser policy (toggle coverage, code-execution opt-in).

Audit findings this covers:
  * `--isolated` was hard-wired, so cookies/logins never survived a run.
  * `browser_snapshot` returned the whole accessibility tree unbounded.
  * The admin "browser off" toggle wrote the server id (`builtin_browser`)
    into `disabled_tools`, which the qualified-name gates never matched.
  * `browser_evaluate` / `browser_run_code_unsafe` were always offered.
"""

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import pytest

import src.settings as settings_mod
from src.mcp_manager import (
    McpManager,
    builtin_browser_policy_disabled,
    truncate_browser_snapshot,
)
from src.tool_capabilities import BROWSER_MCP_ALL_TOOLS, BROWSER_MCP_PREFIX


ROOT = Path(__file__).resolve().parent.parent
BASE = ["-y", "@playwright/mcp@latest"]


def _load_builtin_mcp(monkeypatch):
    core = types.ModuleType("core")
    core.__path__ = []
    platform_compat = types.ModuleType("core.platform_compat")
    platform_compat.IS_WINDOWS = False
    platform_compat.which_tool = lambda name: None
    monkeypatch.setitem(sys.modules, "core", core)
    monkeypatch.setitem(sys.modules, "core.platform_compat", platform_compat)
    spec = importlib.util.spec_from_file_location("builtin_mcp_settings_under_test", ROOT / "src" / "builtin_mcp.py")
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


# ── A. settings → args ────────────────────────────────────────────────────


def test_new_browser_settings_have_the_documented_defaults():
    d = settings_mod.DEFAULT_SETTINGS
    assert d["browser_profile"] == "persistent"
    assert d["browser_cdp_endpoint"] == ""
    assert d["browser_headless"] is True
    assert d["browser_vision_caps"] is False
    assert d["browser_snapshot_max_chars"] == 12000
    assert d["browser_allow_code_execution"] is False
    assert d["browser_live_view"] is True


def test_default_profile_is_persistent_under_data_dir(builtin_mcp):
    args = builtin_mcp._browser_mcp_args(BASE, settings={})

    assert "--isolated" not in args
    assert "--user-data-dir" in args
    profile = _flag_value(args, "--user-data-dir")
    assert profile.endswith("browser-profile")
    assert "--headless" in args
    assert "--caps" not in args  # vision off by default
    assert "--no-sandbox" in args


def test_isolated_profile_setting(builtin_mcp):
    args = builtin_mcp._browser_mcp_args(BASE, settings={"browser_profile": "isolated"})

    assert "--isolated" in args
    assert "--user-data-dir" not in args


def test_headless_off_setting(builtin_mcp):
    args = builtin_mcp._browser_mcp_args(BASE + ["--headless"], settings={"browser_headless": False})

    assert "--headless" not in args


def test_vision_caps_setting_adds_and_removes_vision(builtin_mcp):
    on = builtin_mcp._browser_mcp_args(BASE, settings={"browser_vision_caps": True})
    assert "--caps" in on and "vision" in _flag_value(on, "--caps").split(",")

    # A pre-existing --caps pdf keeps pdf and gains vision
    merged = builtin_mcp._browser_mcp_args(BASE + ["--caps", "pdf"], settings={"browser_vision_caps": True})
    assert set(_flag_value(merged, "--caps").split(",")) == {"pdf", "vision"}

    # Off strips vision but keeps other caps
    off = builtin_mcp._browser_mcp_args(BASE + ["--caps", "pdf,vision"], settings={"browser_vision_caps": False})
    assert _flag_value(off, "--caps") == "pdf"
    only = builtin_mcp._browser_mcp_args(BASE + ["--caps", "vision"], settings={"browser_vision_caps": False})
    assert "--caps" not in only


def test_cdp_endpoint_replaces_launch_flags(builtin_mcp):
    args = builtin_mcp._browser_mcp_args(
        BASE + ["--headless", "--caps", "vision"],
        settings={"browser_cdp_endpoint": "http://127.0.0.1:9222", "browser_vision_caps": True},
    )

    assert "--cdp-endpoint" in args
    assert _flag_value(args, "--cdp-endpoint") == "http://127.0.0.1:9222"
    for flag in ("--headless", "--isolated", "--user-data-dir", "--executable-path", "--no-sandbox"):
        assert flag not in args, flag
    # caps still apply — they shape the tool set, not the launch
    assert "vision" in _flag_value(args, "--caps")


def test_env_no_sandbox_override_kept(builtin_mcp, monkeypatch):
    monkeypatch.setenv("ODYSSEUS_BROWSER_NO_SANDBOX", "0")
    args = builtin_mcp._browser_mcp_args(BASE, settings={})
    assert "--no-sandbox" not in args


def test_browser_launch_args_reads_settings_json(builtin_mcp, monkeypatch):
    monkeypatch.setattr(settings_mod, "load_settings", lambda: {"browser_profile": "isolated", "browser_headless": False})
    args = builtin_mcp.browser_launch_args()
    assert "--isolated" in args
    assert "--headless" not in args


def test_launch_staleness_detects_a_settings_change(builtin_mcp, monkeypatch):
    class _Mgr:
        def __init__(self, launch_args):
            self.statuses = {"builtin_browser": {"status": "connected", "launch_args": launch_args}}

        def get_all_statuses(self):
            return self.statuses

    monkeypatch.setattr(settings_mod, "load_settings", lambda: {"browser_profile": "isolated"})
    current = builtin_mcp.browser_launch_args()
    assert builtin_mcp.browser_launch_is_stale(_Mgr(current)) is False

    monkeypatch.setattr(settings_mod, "load_settings", lambda: {"browser_profile": "persistent"})
    assert builtin_mcp.browser_launch_is_stale(_Mgr(current)) is True
    # Not connected → nothing to restart
    dead = _Mgr(current)
    dead.statuses["builtin_browser"]["status"] = "error"
    assert builtin_mcp.browser_launch_is_stale(dead) is False


async def test_restart_builtin_browser_disconnects_then_reconnects_with_current_args(builtin_mcp, monkeypatch):
    monkeypatch.setattr(settings_mod, "load_settings", lambda: {"browser_profile": "isolated"})
    calls = []

    class _Mgr:
        def __init__(self):
            self.meta = {}

        async def disconnect_server(self, sid):
            calls.append(("disconnect", sid))

        async def connect_server(self, **kw):
            calls.append(("connect", kw["server_id"], kw["args"]))
            return True

        def set_connection_meta(self, sid, **meta):
            self.meta[sid] = meta

        def get_all_statuses(self):
            return {}

    mgr = _Mgr()
    assert await builtin_mcp.restart_builtin_browser(mgr) is True
    assert calls[0] == ("disconnect", "builtin_browser")
    assert calls[1][0] == "connect" and "--isolated" in calls[1][2]
    assert mgr.meta["builtin_browser"]["launch_args"] == calls[1][2]


# ── B. snapshot budget ────────────────────────────────────────────────────


def test_truncate_cuts_at_line_boundary_and_appends_note():
    lines = [f"- line {i} {'x' * 40}" for i in range(400)]
    text = "### Page\n- Page URL: https://example.com\n- Page Title: Example\n### Snapshot\n" + "\n".join(lines)
    out = truncate_browser_snapshot(text, 2000)

    assert len(out) < len(text)
    assert out.startswith("### Page\n- Page URL: https://example.com\n- Page Title: Example")
    body, note = out.rsplit("\n\n", 1)
    assert note == "(snapshot truncated to 2000 chars — use browser_find or browser_snapshot with a narrower scope)"
    assert len(body) <= 2000
    # cut on a whole line: the last kept line is complete
    assert body.splitlines()[-1].endswith("x" * 40)


def test_truncate_leaves_short_text_alone():
    text = "### Page\n- Page URL: about:blank"
    assert truncate_browser_snapshot(text, 12000) is text


def test_call_tool_applies_budget_to_snapshot_but_never_to_errors(monkeypatch):
    mgr = McpManager()
    big = "### Snapshot\n" + "\n".join("- item %d" % i for i in range(5000))

    class _Session:
        def __init__(self, text, is_error):
            self.text, self.is_error = text, is_error

        async def call_tool(self, name, args):
            content = types.SimpleNamespace(type="text", text=self.text)
            return types.SimpleNamespace(content=[content], isError=self.is_error)

    monkeypatch.setattr(settings_mod, "load_settings", lambda: {"browser_snapshot_max_chars": 1000})
    monkeypatch.setattr(mgr, "ensure_builtin_browser_current", _noop_async)

    mgr._sessions["builtin_browser"] = _Session(big, False)
    ok = asyncio.run(mgr.call_tool("mcp__builtin_browser__browser_snapshot", {}))
    assert ok["exit_code"] == 0
    assert len(ok["stdout"]) < 1200
    assert "snapshot truncated to 1000 chars" in ok["stdout"]

    # browser_navigate carries the snapshot too
    mgr._sessions["builtin_browser"] = _Session(big, False)
    nav = asyncio.run(mgr.call_tool("mcp__builtin_browser__browser_navigate", {"url": "x"}))
    assert "snapshot truncated" in nav["stdout"]

    # An error result of the same size is left verbatim
    mgr._sessions["builtin_browser"] = _Session(big, True)
    err = asyncio.run(mgr.call_tool("mcp__builtin_browser__browser_snapshot", {}))
    assert err["exit_code"] == 1
    assert err["stderr"] == big

    # Screenshots are not snapshots
    mgr._sessions["builtin_browser"] = _Session(big, False)
    shot = asyncio.run(mgr.call_tool("mcp__builtin_browser__browser_take_screenshot", {}))
    assert shot["stdout"] == big


async def _noop_async(*a, **k):
    return False


# ── F. policy: toggle coverage + code execution opt-in ────────────────────


def _browser_tools():
    return [{"name": n[len(BROWSER_MCP_PREFIX):], "description": "d", "input_schema": {}} for n in sorted(BROWSER_MCP_ALL_TOOLS)]


def _mgr_with_browser():
    mgr = McpManager()
    mgr._tools["builtin_browser"] = _browser_tools()
    mgr._connections["builtin_browser"] = {"status": "connected", "name": "Built-in: Browser"}
    mgr._sessions["builtin_browser"] = object()
    return mgr


def test_code_execution_tools_are_withheld_by_default(monkeypatch):
    monkeypatch.setattr(settings_mod, "load_settings", lambda: {})
    denied = builtin_browser_policy_disabled()
    assert denied == {"browser_evaluate", "browser_run_code_unsafe"}

    mgr = _mgr_with_browser()
    offered = {s["function"]["name"] for s in mgr.get_all_openai_schemas()}
    assert "mcp__builtin_browser__browser_evaluate" not in offered
    assert "mcp__builtin_browser__browser_run_code_unsafe" not in offered
    assert "mcp__builtin_browser__browser_click" in offered
    prompt = mgr.get_tool_descriptions_for_prompt()
    assert "browser_run_code_unsafe" not in prompt
    assert "browser_click" in prompt

    # ...and refused at dispatch (offered-then-executable invariant)
    res = asyncio.run(mgr.call_tool("mcp__builtin_browser__browser_evaluate", {"function": "() => 1"}))
    assert res["blocked"] is True and res["exit_code"] == 1
    assert "browser_allow_code_execution" in res["error"]


def test_code_execution_opt_in_offers_the_tools(monkeypatch):
    monkeypatch.setattr(settings_mod, "load_settings", lambda: {"browser_allow_code_execution": True})
    assert builtin_browser_policy_disabled() == set()
    mgr = _mgr_with_browser()
    offered = {s["function"]["name"] for s in mgr.get_all_openai_schemas()}
    assert "mcp__builtin_browser__browser_evaluate" in offered
    assert "mcp__builtin_browser__browser_run_code_unsafe" in offered


def test_browser_off_toggle_covers_every_tool_by_server_id(monkeypatch):
    monkeypatch.setattr(settings_mod, "load_settings", lambda: {"disabled_tools": ["builtin_browser"]})
    mgr = _mgr_with_browser()

    assert [s for s in mgr.get_all_openai_schemas() if s["function"]["name"].startswith(BROWSER_MCP_PREFIX)] == []
    assert mgr.get_tool_descriptions_for_prompt() == ""
    assert all(t["is_disabled"] for t in mgr.get_all_tools() if t["server_id"] == "builtin_browser")

    for qualified in ("mcp__builtin_browser__browser_navigate", "mcp__builtin_browser__browser_mouse_click_xy"):
        res = asyncio.run(mgr.call_tool(qualified, {}))
        assert res["blocked"] is True, qualified
        assert "switched off" in res["error"]


def test_prompt_cache_follows_the_policy_without_restart(monkeypatch):
    mgr = _mgr_with_browser()
    monkeypatch.setattr(settings_mod, "load_settings", lambda: {})
    before = mgr.get_tool_descriptions_for_prompt()
    assert "browser_click" in before
    monkeypatch.setattr(settings_mod, "load_settings", lambda: {"disabled_tools": ["builtin_browser"]})
    assert mgr.get_tool_descriptions_for_prompt() == ""
