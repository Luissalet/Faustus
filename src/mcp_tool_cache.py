"""src/mcp_tool_cache.py — per-server MCP tool-schema cache, version-keyed.

A19: what an MCP server's `tools/list` returns can change under a
connected client — the server bumps its `serverInfo.version`, or ships a
schema change while claiming the same version and sends
`notifications/tools/list_changed`. Either way, a schema cached before the
change must never be handed to the model afterwards.

The version key mixes both signals: `serverInfo.version` (catches an
explicit server bump) and a hash of the catalog itself (catches a server
that changes a tool's schema without bumping its version, or one with no
version at all). Any mismatch on either half is a cache miss — the only
way this cache is safe is if a stale entry can never look current.

In-process only (module-level dict): same lifetime and scope as the
`McpManager` singleton it backs, and deliberately without an import
dependency on it — the cache is a plain key/value store, not a client for
mcp_manager to reach into.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# server_id -> {"version_key": str, "tools": [...]}
_cache: Dict[str, Dict[str, Any]] = {}


def compute_catalog_hash(tools: List[Dict[str, Any]]) -> str:
    """Stable hash of a tool catalog: name, description and input schema
    for every tool, sorted by name so discovery order (which pagination
    order does not guarantee) never changes the hash."""
    normalized = sorted(
        (
            {
                "name": t.get("name"),
                "description": t.get("description", ""),
                "input_schema": t.get("input_schema") or {},
            }
            for t in tools
        ),
        key=lambda t: t["name"] or "",
    )
    blob = json.dumps(normalized, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def compute_version_key(server_version: Optional[str], tools: List[Dict[str, Any]]) -> str:
    """The cache key for one discovery: `serverInfo.version` (or
    "unknown" when the server doesn't report one) plus the catalog hash.
    A server that changes either — bumps its version, or ships a
    different schema under the same version — gets a different key."""
    return f"{server_version or 'unknown'}:{compute_catalog_hash(tools)}"


def get(server_id: str) -> Optional[Dict[str, Any]]:
    """The cached {"version_key", "tools"} entry for `server_id`, or None."""
    return _cache.get(server_id)


def is_current(server_id: str, version_key: str) -> bool:
    """True when `server_id`'s cached entry is still at `version_key` —
    i.e. nothing has invalidated it and the last discovery agrees."""
    entry = _cache.get(server_id)
    return bool(entry) and entry.get("version_key") == version_key


def put(server_id: str, version_key: str, tools: List[Dict[str, Any]]) -> None:
    _cache[server_id] = {"version_key": version_key, "tools": tools}


def invalidate(server_id: str) -> None:
    """Drop `server_id`'s cached entry — called on
    `notifications/tools/list_changed` and before a reconnect's fresh
    discovery, so a version mismatch is never served from a stale write
    that happened to race back in first."""
    _cache.pop(server_id, None)


def clear() -> None:
    """Drop every cached entry (tests, full manager reset)."""
    _cache.clear()
