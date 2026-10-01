"""src/chat_bridges/status.py — what the Telegram bridge reports about itself.

The poller runs inside the app; the MCP server (`mcp_servers/chat_bridges_server.py`)
is a separate process. So the poller leaves a small snapshot of its state in
the bridge's sqlite file on every change and every poll, and anything outside
the app reads that (plus the settings and the chat mapping) instead of asking
the live object. A snapshot older than `STALE_AFTER_S` is reported as not
running: a poll takes at most ~25 s, so a silent minute means it is gone.

The token is never part of this: only whether one is saved.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, Optional
from urllib.parse import quote

from .store import BridgeStore

STALE_AFTER_S = 90.0
SNAPSHOT_KEY = "status"


def link(public_url: str, session_id: str) -> str:
    return f"{public_url.rstrip('/')}/studio?s={quote(session_id, safe='')}"


def write_snapshot(store: BridgeStore, live: Dict[str, Any]) -> None:
    payload = dict(live, updated=time.time(), pid=os.getpid())
    store.set_meta(SNAPSHOT_KEY, json.dumps(payload))


def read_snapshot(store: BridgeStore) -> Dict[str, Any]:
    try:
        data = json.loads(store.get_meta(SNAPSHOT_KEY) or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def assemble(cfg: Any, store: BridgeStore, live: Dict[str, Any], *,
             busy_chats: Optional[list] = None, queued: int = 0) -> Dict[str, Any]:
    sessions = [{"chat_id": r["chat_id"], "title": r["title"], "session_id": r["session_id"],
                 "updated": r["updated"], "link": link(cfg.public_url, r["session_id"])}
                for r in store.list_sessions()]
    refused = [{"chat_id": r["chat_id"], "title": r["title"], "at": r["ts"]} for r in store.list_refused()]
    return {
        "enabled": cfg.enabled,
        "configured": bool(cfg.token),
        "running": bool(live.get("running")),
        "bot_username": live.get("bot_username", ""),
        "bot_name": live.get("bot_name", ""),
        "last_error": live.get("last_error", ""),
        "last_error_at": live.get("last_error_at", 0.0),
        "disabled_reason": live.get("disabled_reason", ""),
        "failures_in_a_row": live.get("failures_in_a_row", 0),
        "mode": cfg.mode,
        "model": cfg.model,
        "allowed_chats": sorted(cfg.allowed),
        "mapped_sessions": sessions,
        "refused_chats": refused,
        "busy_chats": busy_chats or [],
        "queued_messages": queued,
    }


def read_status(store: Optional[BridgeStore] = None, cfg: Any = None) -> Dict[str, Any]:
    """The bridge's state as seen from outside the app process."""
    if cfg is None:
        from .telegram_bridge import load_config
        cfg = load_config()
    store = store or BridgeStore()
    snap = read_snapshot(store)
    age = time.time() - float(snap.get("updated") or 0)
    live = dict(snap)
    live["running"] = bool(snap.get("running")) and age <= STALE_AFTER_S
    out = assemble(cfg, store, live)
    out["snapshot_age_s"] = round(age, 1) if snap else None
    return out
