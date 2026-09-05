"""Live browser view (audit finding 6, UI half).

`src/browser_view.py` takes one viewport frame after each browser ACTION and
returns {url, title, screenshot}; the agent loop turns it into a
`browser_view` SSE event for the side panel. The frame is for the UI only —
it must never land in the tool result's `images` (what the model reads).

The panel's own half — the raster whitelist a frame must pass, the bounded
frame list, the live marker, desktop frames — is pinned in
tests/test_studio_panel_js.py.
"""

import json
from pathlib import Path

import pytest

from src import browser_view as bv


ROOT = Path(__file__).resolve().parent.parent
P = "mcp__builtin_browser__"
JPEG = "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAABAAAAAAAAAAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q=="

NAV_TEXT = (
    "### Ran Playwright code\n```js\nawait page.goto('https://example.com/');\n```\n"
    "### Page\n- Page URL: https://example.com/\n- Page Title: Example Domain\n"
    "### Snapshot\n- [Snapshot](.playwright-mcp/page.yml)\n"
)
TABS_TEXT = "### Result\n- 0: (current) [Example Domain](https://example.com/)\n- 1: [Other](https://other.test/)\n"


class FakeMgr:
    def __init__(self, shot=None, tabs=None):
        self.calls = []
        self._shot = shot if shot is not None else {
            "stdout": "### Result\n- [Screenshot of viewport](.playwright-mcp/page.jpeg)",
            "exit_code": 0,
            "images": [{"data": JPEG, "mimeType": "image/jpeg"}],
        }
        self._tabs = tabs if tabs is not None else {"stdout": TABS_TEXT, "exit_code": 0}

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        if name.endswith("browser_take_screenshot"):
            return self._shot
        if name.endswith("browser_tabs"):
            return self._tabs
        return {"error": "unexpected", "exit_code": 1}


# ── parsing ───────────────────────────────────────────────────────────────


def test_parse_page_info_reads_playwright_lines():
    assert bv.parse_page_info(NAV_TEXT) == ("https://example.com/", "Example Domain")
    assert bv.parse_page_info("### Page\n- Page URL: about:blank\n") == ("about:blank", "")
    assert bv.parse_page_info("") == ("", "")
    assert bv.parse_page_info(None) == ("", "")


def test_parse_tabs_current():
    assert bv.parse_tabs_current(TABS_TEXT) == ("https://example.com/", "Example Domain")
    assert bv.parse_tabs_current("### Result\n- 0: [x](y)\n") == ("", "")


def test_action_classification():
    for name in ("browser_navigate", "browser_navigate_back", "browser_click", "browser_type", "browser_fill_form",
                 "browser_select_option", "browser_press_key", "browser_hover", "browser_drag", "browser_drop",
                 "browser_tabs", "browser_handle_dialog", "browser_mouse_click_xy", "browser_mouse_wheel"):
        assert bv.is_browser_action(P + name), name
    for name in ("browser_snapshot", "browser_take_screenshot", "browser_find", "browser_console_messages",
                 "browser_network_requests", "browser_wait_for", "browser_close"):
        assert not bv.is_browser_action(P + name), name
    assert not bv.is_browser_action("bash")
    assert not bv.is_browser_action(None)


# ── after_browser_action ──────────────────────────────────────────────────


async def test_event_shape_after_navigate_uses_result_text_for_url_title():
    mgr = FakeMgr()
    result = {"stdout": NAV_TEXT, "exit_code": 0}
    out = await bv.after_browser_action(P + "browser_navigate", result, mgr, {"browser_live_view": True})

    assert out == {
        "url": "https://example.com/",
        "title": "Example Domain",
        "screenshot": f"data:image/jpeg;base64,{JPEG}",
    }
    # viewport jpeg only; url/title came from the result → no tabs call
    assert mgr.calls == [(P + "browser_take_screenshot", {"type": "jpeg"})]
    # the action's own result is untouched (nothing for the model)
    assert "images" not in result


async def test_falls_back_to_tabs_list_when_result_has_no_page_lines():
    mgr = FakeMgr()
    out = await bv.after_browser_action(P + "browser_click", {"stdout": "### Ran Playwright code\n```js\nawait page.click()\n```", "exit_code": 0}, mgr, {})
    assert out["url"] == "https://example.com/"
    assert out["title"] == "Example Domain"
    assert [c[0] for c in mgr.calls] == [P + "browser_take_screenshot", P + "browser_tabs"]
    assert mgr.calls[1][1] == {"action": "list"}


async def test_no_frame_for_observation_tools_or_when_disabled_or_blocked():
    mgr = FakeMgr()
    assert await bv.after_browser_action(P + "browser_snapshot", {"stdout": NAV_TEXT}, mgr, {}) is None
    assert await bv.after_browser_action(P + "browser_navigate", {"stdout": NAV_TEXT}, mgr, {"browser_live_view": False}) is None
    assert await bv.after_browser_action(P + "browser_navigate", {"error": "x", "blocked": True, "exit_code": 1}, mgr, {}) is None
    assert await bv.after_browser_action(P + "browser_navigate", {"stdout": NAV_TEXT}, None, {}) is None
    assert mgr.calls == []


async def test_no_frame_for_an_action_that_is_only_pending_approval():
    """Audited: a click that stopped at the approval card still produced a
    live-view frame (and a screenshot round-trip) — the action did not run,
    so there is nothing new to show; the frame only suggested it had."""
    mgr = FakeMgr()
    pending = {"output": "Waiting for an exact user approval.", "exit_code": None,
               "approval_required": True, "ask_user": {}}
    assert await bv.after_browser_action(P + "browser_click", pending, mgr, {"browser_live_view": True}) is None
    assert mgr.calls == []
    # a failed action, by contrast, still shows where the page ended up
    errored = {"error": "Timeout 30000ms exceeded", "exit_code": 1}
    out = await bv.after_browser_action(P + "browser_navigate", errored, mgr, {"browser_live_view": True})
    assert out is not None and out["screenshot"].startswith("data:image/jpeg;base64,")


async def test_screenshot_failure_is_swallowed():
    class Boom(FakeMgr):
        async def call_tool(self, name, args):
            raise RuntimeError("browser gone")

    assert await bv.after_browser_action(P + "browser_navigate", {"stdout": NAV_TEXT}, Boom(), {}) is None
    no_image = FakeMgr(shot={"stdout": "### Error\nno page", "exit_code": 1})
    assert await bv.after_browser_action(P + "browser_navigate", {"stdout": NAV_TEXT}, no_image, {}) is None


async def test_control_characters_are_stripped_from_url_and_title():
    mgr = FakeMgr()
    text = "### Page\n- Page URL: https://e.test/\x1b[31m\n- Page Title: Bad\x00Title\n"
    out = await bv.after_browser_action(P + "browser_navigate", {"stdout": text}, mgr, {})
    assert "\x1b" not in out["url"] and "\x00" not in out["title"]


# ── wiring: agent loop + chat route whitelist ─────────────────────────────


def test_agent_loop_emits_browser_view_event_and_attaches_frame_to_card():
    src = (ROOT / "src" / "agent_loop.py").read_text(encoding="utf-8")
    assert "from src.browser_view import after_browser_action" in src
    assert '"type": "browser_view", "tool": block.tool_type, **_browser_view' in src
    # the card gets the frame, the model does not (no result["images"] write)
    assert 'tool_output_data["screenshot"] = _browser_view["screenshot"]' in src
    assert 'result["images"] = ' not in src.split("Live browser view")[1].split("Forward a file-write diff")[0]


def test_chat_route_forwards_browser_view_events():
    src = (ROOT / "routes" / "chat_routes.py").read_text(encoding="utf-8")
    assert '"browser_view",' in src
