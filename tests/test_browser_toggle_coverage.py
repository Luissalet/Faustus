"""Audit finding 4: "browser off" must cover EVERY mcp__builtin_browser__* tool.

Before: the agent toggle wrote the server id `builtin_browser` into
`disabled_tools` (the gates compare qualified names → nothing blocked) and
`can_use_browser=False` listed 12 of the 30 Playwright tools by hand.
"""

import sys
import types

import pytest

from src.tool_capabilities import (
    BROWSER_MCP_ALL_TOOLS,
    BROWSER_MCP_PREFIX,
    browser_tool_denials,
)


P = BROWSER_MCP_PREFIX
_LIVE_EXTRA = P + "browser_some_future_tool"


# ── the shared expansion helper ───────────────────────────────────────────


def test_static_browser_tool_set_is_complete():
    """Every tool @playwright/mcp 0.0.80 exposes (with --caps vision)."""
    expected = {
        "browser_close", "browser_resize", "browser_console_messages", "browser_handle_dialog",
        "browser_evaluate", "browser_file_upload", "browser_drop", "browser_find", "browser_fill_form",
        "browser_press_key", "browser_type", "browser_mouse_move_xy", "browser_mouse_click_xy",
        "browser_mouse_drag_xy", "browser_mouse_down", "browser_mouse_up", "browser_mouse_wheel",
        "browser_navigate", "browser_navigate_back", "browser_network_requests", "browser_network_request",
        "browser_run_code_unsafe", "browser_take_screenshot", "browser_snapshot", "browser_click",
        "browser_drag", "browser_hover", "browser_select_option", "browser_tabs", "browser_wait_for",
    }
    assert {n[len(P):] for n in BROWSER_MCP_ALL_TOOLS} == expected
    assert len(BROWSER_MCP_ALL_TOOLS) == 30


def test_browser_tool_denials_expands_server_id_to_static_and_live_names():
    got = browser_tool_denials({"builtin_browser", "bash"}, live_tool_names=[_LIVE_EXTRA, "read_file"])
    assert got >= BROWSER_MCP_ALL_TOOLS
    assert _LIVE_EXTRA in got
    assert "read_file" not in got and "bash" not in got


def test_browser_tool_denials_wildcard_and_no_match():
    assert browser_tool_denials({P + "*"}) == BROWSER_MCP_ALL_TOOLS
    assert browser_tool_denials({"bash", P + "browser_click"}) == frozenset()
    assert browser_tool_denials(None) == frozenset()


# ── chat route: can_use_browser=False ─────────────────────────────────────


def test_chat_route_browser_denylist_covers_all_tools_by_prefix(monkeypatch):
    import routes.chat_routes as cr

    class _Mgr:
        def browser_tool_names(self):
            return {_LIVE_EXTRA, P + "browser_click"}

    monkeypatch.setattr(cr, "get_mcp_manager", lambda: _Mgr(), raising=False)
    denied = cr._browser_mcp_denylist()

    assert denied >= BROWSER_MCP_ALL_TOOLS
    assert _LIVE_EXTRA in denied
    assert all(n.startswith(P) for n in denied)


def test_chat_route_static_list_is_no_longer_a_hand_picked_subset():
    import routes.chat_routes as cr

    assert set(cr._BROWSER_MCP_TOOLS) == set(BROWSER_MCP_ALL_TOOLS)
    for name in ("browser_evaluate", "browser_run_code_unsafe", "browser_console_messages",
                 "browser_hover", "browser_tabs", "browser_resize", "browser_mouse_click_xy"):
        assert P + name in cr._BROWSER_MCP_TOOLS, name


def test_chat_route_privilege_gate_uses_the_prefix_denylist():
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent / "routes" / "chat_routes.py").read_text(encoding="utf-8")
    assert 'if not _privs.get("can_use_browser", True):\n                disabled_tools.update(_browser_mcp_denylist())' in source


# ── admin toggle: manage_settings disable_tool browser ────────────────────


@pytest.mark.asyncio
async def test_disable_tool_browser_covers_every_qualified_tool(monkeypatch):
    from src.tool_implementations import do_manage_settings
    import src.settings as settings_mod

    db_mod = types.ModuleType("core.database")

    class _Db:
        def close(self):
            pass

    db_mod.SessionLocal = lambda: _Db()
    monkeypatch.setitem(sys.modules, "core.database", db_mod)

    store = {}
    monkeypatch.setattr(settings_mod, "load_settings", lambda: dict(store))

    def fake_save(s):
        store.clear()
        store.update(s)

    monkeypatch.setattr(settings_mod, "save_settings", fake_save)

    class _Mgr:
        def browser_tool_names(self):
            return {_LIVE_EXTRA}

    import src.agent_tools.admin_tools as admin_tools
    monkeypatch.setattr(admin_tools, "get_mcp_manager", lambda: _Mgr())

    result = await do_manage_settings('{"action": "disable_tool", "tool": "browser"}', owner="admin")
    assert result["exit_code"] == 0
    disabled = set(store["disabled_tools"])
    assert "builtin_browser" in disabled  # server id keeps the McpManager-side gate
    assert disabled >= BROWSER_MCP_ALL_TOOLS
    assert _LIVE_EXTRA in disabled

    result = await do_manage_settings('{"action": "enable_tool", "tool": "browser"}', owner="admin")
    assert result["exit_code"] == 0
    assert store["disabled_tools"] == []
