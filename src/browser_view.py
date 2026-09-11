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

The other direction — provenance
--------------------------------
:func:`annotate_page_text` is the seam where page text becomes model-visible
content. When ``agent_web_provenance`` is on (default) it hands back a COPY of
the tool result whose text carries one ``<!-- source: … -->`` comment per
block (``src/web_provenance.py``), so the model can cite what it read and the
user can check the citation against the page. The anchor is a character range
plus a content hash, never a pixel coordinate — see that module's docstring.

It returns ``None`` whenever nothing should change: the setting off, a tool
that returns no page text, a refused or parked action, a result with no URL to
anchor to. A caller that keeps its own result object on ``None`` therefore
produces a byte-identical run with the setting off.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Mapping, Optional, Tuple

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

# Tools whose text result IS page content the model reads. Console, network
# and tab listings are the browser talking about itself, not the page, so
# anchoring them to a URL would be a claim about the page that is not true.
BROWSER_TEXT_TOOLS = frozenset(
    {
        "browser_snapshot",
        "browser_navigate",
        "browser_navigate_back",
    }
)

# The result keys a browser tool may put its text in, in the order the rest of
# this module already looks at them.
_TEXT_KEYS = ("stdout", "output")

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


def _strip_list_marker(line: str) -> str:
    """Drop a leading tree dash (``- ``) or numbered-list marker (``1. ``,
    ``2) ``) — both appear in real snapshot text depending on which
    ``@playwright/mcp`` build (or relay) produced it, and neither carries
    information the element parser below needs."""
    return re.sub(r'^\s*(?:[-*•]|\d{1,4}[.)])\s*', '', line)


_ELEMENT_REF_RE = re.compile(r'ref[:=]\s*["\']?([A-Za-z0-9_-]+)', re.IGNORECASE)
_ELEMENT_NAME_QUOTED_RE = re.compile(r'"([^"]*)"')
# The role is whatever comes before the name/attributes/colon/ref start —
# letters and spaces only (an ARIA role is never anything else: "list item",
# "menu item", "radio group"...), so this stops at the first character that
# can only belong to a name, a bracket/paren attribute list, a trailing
# colon, or a bare `ref=`/`ref:` with no colon before it at all (Format B's
# "banner ref=e2", which has no name to separate the role from).
_ELEMENT_ROLE_RE = re.compile(r'^([A-Za-z][A-Za-z ]*?)(?=\s*(?:"|\[|\(|:|\bref[:=]|$))',
                              re.IGNORECASE)


def parse_snapshot_elements(text: Any) -> List[Dict[str, str]]:
    """Every ``{ref, role, name}`` this module can find in one `browser_
    snapshot` result, independent of which of the two real
    ``@playwright/mcp`` shapes produced the text (WEB-04).

    Format A — the current aria-snapshot tree, one indented line per node,
    the ref among a trailing run of bracketed attributes::

        - button "Delete item" [ref=e7] [cursor=pointer]

    Format B — a flat, non-indented relay format (an older server build, or
    an MCP bridge that re-renders the tree as a numbered list) with the ref
    in parentheses and the name after a colon instead of in quotes::

        5. button: Delete item (ref=e7)

    Only a line that actually carries a ``ref=``/``ref:`` token becomes an
    element — a line with none (page URL/title headers, prose) is silently
    skipped rather than guessed at. Never raises: a line neither shape can
    parse is skipped, not fatal to the rest of the snapshot.
    """
    if not isinstance(text, str) or not text:
        return []
    out = []
    for raw_line in text.splitlines():
        line = _strip_list_marker(raw_line.strip())
        if not line:
            continue
        ref_m = _ELEMENT_REF_RE.search(line)
        if not ref_m:
            continue
        ref = ref_m.group(1)
        name_m = _ELEMENT_NAME_QUOTED_RE.search(line)
        name = name_m.group(1) if name_m else ""
        role_m = _ELEMENT_ROLE_RE.match(line)
        role = role_m.group(1).strip() if role_m else ""
        if not name and ":" in line:
            # Format B's "role: name (ref=...)" — the name has no quotes,
            # it is whatever sits between the colon and the ref/attribute
            # tail that follows it.
            after = line.split(":", 1)[1]
            after = re.sub(r'\(?\s*ref[:=].*', '', after, flags=re.IGNORECASE)
            after = re.sub(r'\[.*', '', after)
            name = after.strip().strip('"').rstrip(")")
        out.append({"ref": ref, "role": role, "name": name})
    return out


def element_present(ref: Any, snapshot_text: Any) -> bool:
    """Is `ref` still an element in this FRESH snapshot — checked by exact
    ref identity, not by whether the string happens to appear somewhere in
    the text.

    That distinction is WEB-04's whole point: a snapshot whose old ``ref=e1``
    is gone but a NEW, unrelated ``ref=e10`` exists would satisfy a bare
    substring check (``"e1" in text``) — the check this replaces — and would
    then let a stale reference through as "still present". Exact-match
    parsing via `parse_snapshot_elements` catches exactly that case.

    Falls back to the old substring check only when NOTHING in the snapshot
    parses as an element at all (an unrecognized third shape, or truncated
    text) — refusing every action outright on a shape the parser has never
    seen would cost more than the precision this function exists to add.
    """
    ref = str(ref or "").strip()
    if not ref:
        return True
    elements = parse_snapshot_elements(snapshot_text)
    if elements:
        return any(e["ref"] == ref for e in elements)
    return ref in str(snapshot_text or "")


#: ADP-29: a subtree must come back genuinely smaller than the page — this
#: is the hard stop for a snapshot whose indentation never returns to the
#: root's level (a malformed or adversarial page), not a size any real
#: subtree is expected to reach.
MAX_SUBTREE_LINES = 4000

#: ADP-29 acceptance: at most this many hits from `search()`, so "search the
#: page" cannot become "dump the page under a different name".
MAX_SEARCH_RESULTS = 200
_DEFAULT_SEARCH_LIMIT = 20


def subtree(snapshot: Any, ref: Any, depth: int = 2) -> str:
    """The lines of `snapshot` rooted at the element whose ref matches
    `ref` EXACTLY (the same identity rule `element_present` uses for WEB-04
    — never a substring), plus up to `depth` further levels of descendants
    by indentation.

    Depth is counted generically from the indentation actually present —
    not a hard-coded step size — by tracking, line by line, which prior
    indent values are still open ancestors: a line whose indent is shallower
    than the root's own ends the subtree; a line at or below the current
    innermost open ancestor's indent closes that ancestor (sibling or
    uncle) before its own level is computed. That lets this work on
    real aria-snapshot output without assuming "2 spaces per level".

    `depth=0` returns only the matched line, no children. A snapshot with no
    indentation to derive children from (the flat, non-indented relay shape
    `parse_snapshot_elements` also has to handle) degrades to the same
    thing: there is no descendant to bound.

    Returns `""` when `ref` is not present or the input is empty/not a
    string — a caller must not be able to widen an absent ref into "the
    whole page" by asking for it. Never raises.
    """
    if not isinstance(snapshot, str) or not snapshot:
        return ""
    ref = str(ref or "").strip()
    if not ref:
        return ""
    try:
        depth = max(0, int(depth))
    except (TypeError, ValueError):
        depth = 0
    lines = snapshot.splitlines()
    root_idx: Optional[int] = None
    root_indent = 0
    for i, raw in enumerate(lines):
        if not raw.strip():
            continue
        stripped = raw.lstrip(" ")
        m = _ELEMENT_REF_RE.search(stripped)
        if m and m.group(1) == ref:
            root_idx = i
            root_indent = len(raw) - len(stripped)
            break
    if root_idx is None:
        return ""

    out = [lines[root_idx]]
    ancestor_indents: List[int] = []       # open ancestor indents, shallow→deep
    for raw in lines[root_idx + 1:]:
        if not raw.strip():
            continue
        stripped = raw.lstrip(" ")
        indent = len(raw) - len(stripped)
        if indent <= root_indent:
            break  # back to the root's own level or shallower: subtree ends
        while ancestor_indents and indent <= ancestor_indents[-1]:
            ancestor_indents.pop()
        ancestor_indents.append(indent)
        if len(ancestor_indents) <= depth:
            out.append(raw)
            if len(out) >= MAX_SUBTREE_LINES:
                break
    return "\n".join(out)


def search(snapshot: Any, query: Any, limit: int = _DEFAULT_SEARCH_LIMIT) -> List[Dict[str, str]]:
    """`{ref, role, name}` for every element in `snapshot` whose role or
    name contains `query` (case-insensitive substring), in snapshot order,
    capped at `limit`.

    Built on `parse_snapshot_elements` — the same parser `element_present`
    trusts for WEB-04 — so a hit here is guaranteed to be a real, freshly
    parsed element, never a phrase the parser did not recognise as one
    (prose, a URL/title header line). This and `subtree()` are ADP-29's
    answer to "ask for a subtree or a search instead of the whole page":
    a caller after one control does not need — and should not spend the
    context budget on — the full snapshot dump.

    Returns `[]` for an empty/blank query or empty input. Never raises.
    """
    if not isinstance(query, str) or not query.strip():
        return []
    needle = query.strip().lower()
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = _DEFAULT_SEARCH_LIMIT
    limit = max(1, min(limit, MAX_SEARCH_RESULTS))
    out: List[Dict[str, str]] = []
    for element in parse_snapshot_elements(snapshot):
        if needle in element["role"].lower() or needle in element["name"].lower():
            out.append(element)
            if len(out) >= limit:
                break
    return out


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


# ---------------------------------------------------------------------------
# Provenance: the seam where page text becomes model-visible content
# ---------------------------------------------------------------------------


def is_page_text_tool(tool_name: Any) -> bool:
    """True for the browser tools whose text result is page CONTENT."""
    return (
        isinstance(tool_name, str)
        and tool_name.startswith(BROWSER_MCP_PREFIX)
        and tool_name[len(BROWSER_MCP_PREFIX):] in BROWSER_TEXT_TOOLS
    )


def provenance_enabled(settings: Optional[Mapping[str, Any]] = None) -> bool:
    from src.web_provenance import enabled
    return enabled(settings)


def annotate_page_text(
    tool_name: Any,
    result: Any,
    settings: Optional[Mapping[str, Any]] = None,
    *,
    url: str = "",
    fetched_at: str = "",
) -> Optional[dict]:
    """A COPY of ``result`` whose page text carries provenance anchors.

    Returns ``None`` — meaning "keep the result you already have, untouched" —
    when the setting is off, the tool returns no page text, the action was
    refused or is waiting at the approval card, or there is no URL to anchor
    to. That is what makes a setting-off run byte-identical: on ``None`` the
    caller never rebuilds the object.

    ``url`` and ``fetched_at`` are injectable so this is testable without a
    clock; by default the URL comes from the result's own ``- Page URL:`` line
    and the timestamp is now, in UTC.

    Never raises.
    """
    try:
        if not is_page_text_tool(tool_name):
            return None
        if not isinstance(result, dict):
            return None
        if result.get("blocked") or result.get("approval_required"):
            return None
        if not provenance_enabled(settings):
            return None

        key = ""
        text = ""
        for candidate in _TEXT_KEYS:
            value = result.get(candidate)
            if isinstance(value, str) and value.strip():
                key, text = candidate, value
                break
        if not key:
            return None

        page_url = url or parse_page_info(text)[0]
        if not page_url:
            return None
        if "<!-- source:" in text:
            return None  # already anchored; never anchor an anchor

        from datetime import datetime, timezone

        from src import web_provenance

        stamp = fetched_at or datetime.now(timezone.utc).isoformat()
        annotated = web_provenance.annotate(text, url=page_url, fetched_at=stamp)
        if annotated == text:
            return None

        out = dict(result)
        out[key] = annotated
        out["provenance"] = {
            "url": page_url,
            "fetched_at": stamp,
            "blocks": len(web_provenance.extract_provenance(annotated)),
            "anchor": "character range + sha256 of the fetched text, not pixels",
        }
        return out
    except Exception as exc:  # noqa: BLE001 - provenance is additive, never fatal
        logger.debug(f"[browser-view] provenance skipped for {tool_name}: {exc}")
        return None
