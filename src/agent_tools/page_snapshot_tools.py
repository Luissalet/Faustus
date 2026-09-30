"""agent_tools/page_snapshot_tools.py — `page_find` and `page_window`.

Both read the latest browser snapshot of THIS session's browser connection
(or the shared one) from `src.browser_snapshot_window`; neither talks to the
browser. `page_find` searches the whole stored snapshot for a string or a
regular expression; `page_window` returns another window of it.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

from src import browser_snapshot_window as bsw

logger = logging.getLogger(__name__)

_NO_SNAPSHOT = ("no browser snapshot is stored yet for this session — call browser_snapshot "
                "or browser_navigate first")


def _args(content: Any) -> Dict[str, Any]:
    raw = (content or "").strip() if isinstance(content, str) else content
    if isinstance(raw, dict):
        return dict(raw)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {"query": raw} if isinstance(raw, str) else {}
    return parsed if isinstance(parsed, dict) else {}


def _candidate_servers(ctx: dict) -> List[str]:
    from src.tool_capabilities import BROWSER_MCP_SERVER_ID
    ids: List[str] = []
    owner = str((ctx or {}).get("owner") or "").strip()
    session = str((ctx or {}).get("session_id") or "").strip()
    if owner and session:
        try:
            from src.builtin_mcp import session_browser_server_id
            ids.append(session_browser_server_id(owner, session))
        except Exception:  # noqa: BLE001
            pass
    ids.append(BROWSER_MCP_SERVER_ID)
    return ids


def _error(tool: str, message: str) -> dict:
    return {"error": f"{tool}: {message}", "exit_code": 1, "error_class": f"{tool}.invalid"}


def _window_size() -> int:
    from src.mcp_manager import _browser_snapshot_budget
    return max(bsw.MIN_WINDOW, _browser_snapshot_budget())


class PageFindTool:
    """`page_find` {query, regex?, case_sensitive?, context?, max_matches?, snapshot_id?}."""

    async def execute(self, content: Any, ctx: dict) -> dict:
        args = _args(content)
        snap = bsw.latest(_candidate_servers(ctx))
        if snap is None:
            return _error("page_find", _NO_SNAPSHOT)
        wanted = str(args.get("snapshot_id") or "").strip()
        if wanted and wanted != snap.snapshot_id:
            return _error("page_find", f"snapshot {wanted} is not the latest (now {snap.snapshot_id}); "
                                       "omit snapshot_id to search the current page")
        try:
            result = bsw.find_in_snapshot(
                snap, str(args.get("query") or ""), regex=bool(args.get("regex")),
                case_sensitive=bool(args.get("case_sensitive")),
                context=int(args.get("context", 1)),
                max_matches=int(args.get("max_matches", bsw.FIND_DEFAULT_MATCHES)),
            )
        except (bsw.FindError, TypeError, ValueError) as exc:
            return _error("page_find", str(exc))
        return {"output": bsw.format_find(str(args.get("query") or ""), result), "exit_code": 0,
                "total_matches": result["total_matches"], "snapshot_id": result["snapshot_id"],
                "matches": result["matches"]}


class PageWindowTool:
    """`page_window` {offset, limit?, snapshot_id?}: another window of the latest snapshot."""

    async def execute(self, content: Any, ctx: dict) -> dict:
        args = _args(content)
        snap = bsw.latest(_candidate_servers(ctx))
        if snap is None:
            return _error("page_window", _NO_SNAPSHOT)
        wanted = str(args.get("snapshot_id") or "").strip()
        if wanted and wanted != snap.snapshot_id:
            return _error("page_window", f"snapshot {wanted} is not the latest (now {snap.snapshot_id}); "
                                         "take a new browser_snapshot and start from offset 0")
        try:
            offset = int(args.get("offset", 0))
            limit = int(args.get("limit") or _window_size())
        except (TypeError, ValueError):
            return _error("page_window", "`offset` and `limit` must be integers")
        if offset < 0 or offset > len(snap.shown):
            return _error("page_window", f"`offset` must be between 0 and {len(snap.shown)}")
        if not snap.nav:
            snap.nav = bsw.nav_block(snap.raw)
        text = bsw.render_window(snap, offset, limit)
        return {"output": text, "exit_code": 0, "snapshot_id": snap.snapshot_id,
                "total_chars": len(snap.shown)}
