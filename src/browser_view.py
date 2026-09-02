"""Live browser view — what the user sees while the agent drives the browser.

Audit finding 6: the user only ever saw a screenshot when the MODEL asked for
one. After every browser ACTION this module grabs one viewport frame through
the MCP manager and hands back `{url, title, screenshot}` for the UI's
Browser panel (static/js/browserView.js). The frame is for the UI only: it is
never attached to the tool result the model reads (`images`), so it costs the
model nothing and a vision model is not fed a frame it did not ask for.

URL and title come from the action's own text result — Playwright prints
`- Page URL:` / `- Page Title:` lines — and fall back to one cheap
`browser_tabs list` call when the result carries neither.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Mapping, Optional, Tuple

from src.tool_capabilities import BROWSER_MCP_PREFIX

logger = logging.getLogger(__name__)

# Tools that (may) change what the page shows. Observation tools (snapshot,
# screenshot, find, console/network, wait_for, close) take no frame.
BROWSER_VIEW_ACTIONS = frozenset(
    {
        "browser_navigate",
        "browser_navigate_back",
        "browser_click",
        "browser_type",
        "browser_fill_form",
        "browser_select_option",
        "browser_press_key",
        "browser_hover",
        "browser_drag",
        "browser_drop",
        "browser_tabs",
        "browser_handle_dialog",
        "browser_mouse_move_xy",
        "browser_mouse_click_xy",
        "browser_mouse_drag_xy",
        "browser_mouse_down",
        "browser_mouse_up",
        "browser_mouse_wheel",
        # also alter the page, so the user should see the outcome
        "browser_file_upload",
        "browser_resize",
        "browser_evaluate",
        "browser_run_code_unsafe",
    }
)

SCREENSHOT_TOOL = BROWSER_MCP_PREFIX + "browser_take_screenshot"
TABS_TOOL = BROWSER_MCP_PREFIX + "browser_tabs"

_URL_RE = re.compile(r"^\s*-\s*Page URL:\s*(.+?)\s*$", re.MULTILINE)
_TITLE_RE = re.compile(r"^\s*-\s*Page Title:\s*(.*?)\s*$", re.MULTILINE)
# browser_tabs list → "- 0: (current) [Title](url)"
_TABS_CURRENT_RE = re.compile(r"^\s*-\s*\d+:\s*\(current\)\s*\[(.*?)\]\((.*?)\)\s*$", re.MULTILINE)
_MAX_URL = 2048
_MAX_TITLE = 300


def is_browser_action(tool_name: Any) -> bool:
    return (
        isinstance(tool_name, str)
        and tool_name.startswith(BROWSER_MCP_PREFIX)
        and tool_name[len(BROWSER_MCP_PREFIX):] in BROWSER_VIEW_ACTIONS
    )


def live_view_enabled(settings: Optional[Mapping[str, Any]]) -> bool:
    if settings is None:
        try:
            from src.settings import get_setting
            return bool(get_setting("browser_live_view", True))
        except Exception:
            return True
    try:
        value = settings.get("browser_live_view", True)
    except AttributeError:
        return True
    return bool(value) if value is not None else True


def _clean(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", text)
    return text[:limit]


def parse_page_info(text: Any) -> Tuple[str, str]:
    """(url, title) from a Playwright result text; empty strings when absent."""
    if not isinstance(text, str) or not text:
        return "", ""
    url_m = _URL_RE.search(text)
    title_m = _TITLE_RE.search(text)
    return (
        _clean(url_m.group(1), _MAX_URL) if url_m else "",
        _clean(title_m.group(1), _MAX_TITLE) if title_m else "",
    )


def parse_tabs_current(text: Any) -> Tuple[str, str]:
    """(url, title) of the current tab from a `browser_tabs list` result."""
    if not isinstance(text, str) or not text:
        return "", ""
    m = _TABS_CURRENT_RE.search(text)
    if not m:
        return "", ""
    return _clean(m.group(2), _MAX_URL), _clean(m.group(1), _MAX_TITLE)


def _result_text(result: Any) -> str:
    if not isinstance(result, dict):
        return ""
    for key in ("stdout", "output", "stderr"):
        value = result.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _screenshot_data_url(shot: Any) -> str:
    if not isinstance(shot, dict):
        return ""
    images = shot.get("images")
    if not isinstance(images, list) or not images:
        return ""
    img = images[0] if isinstance(images[0], dict) else {}
    data = img.get("data")
    if not isinstance(data, str) or not data.strip():
        return ""
    mime = str(img.get("mimeType") or "image/jpeg").strip().lower()
    if mime not in ("image/jpeg", "image/png", "image/webp", "image/gif"):
        mime = "image/jpeg"
    return f"data:{mime};base64,{data.strip()}"


async def after_browser_action(
    tool_name: str,
    result: Any,
    mcp_manager: Any,
    settings: Optional[Mapping[str, Any]] = None,
) -> Optional[dict]:
    """One viewport frame after a browser action, for the UI panel.

    Returns ``{"url", "title", "screenshot"}`` (screenshot as a data URL) or
    None when the tool is not an action, the live view is off, the action was
    refused or is still waiting for approval, or no frame could be taken.
    Never raises.
    """
    if not is_browser_action(tool_name):
        return None
    if not live_view_enabled(settings):
        return None
    if mcp_manager is None or not hasattr(mcp_manager, "call_tool"):
        return None
    if isinstance(result, dict) and (result.get("blocked") or result.get("approval_required")):
        # Refused, or parked at the approval card: the action did not run,
        # so a frame would only suggest it had (and cost a screenshot).
        return None

    url, title = parse_page_info(_result_text(result))
    try:
        shot = await mcp_manager.call_tool(SCREENSHOT_TOOL, {"type": "jpeg"})
    except Exception as exc:  # noqa: BLE001 - the view is best effort
        logger.debug(f"[browser-view] screenshot after {tool_name} failed: {exc}")
        return None
    screenshot = _screenshot_data_url(shot)
    if not screenshot:
        return None

    if not url:
        try:
            tabs = await mcp_manager.call_tool(TABS_TOOL, {"action": "list"})
            url, tab_title = parse_tabs_current(_result_text(tabs))
            title = title or tab_title
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[browser-view] tabs lookup after {tool_name} failed: {exc}")

    return {"url": url, "title": title, "screenshot": screenshot}
