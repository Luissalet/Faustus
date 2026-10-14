"""Copilot turns from a Hoard: what "this" means and which app tools to offer.

A turn sent from a Hoard's Faustus panel carries the page context (origin app,
view, active objects, unsaved form fields). Two things keep the agent on that
context instead of wandering to desktop windows or other apps:

* ``origin_hint_message`` tells the model, in plain words, that deictic
  references ("this", "here", "what I have open") mean the objects in the
  context, and that it should answer from the context when it suffices and use
  the origin app's own tools to read or change them.
* ``origin_app_tools`` finds the connected MCP server of the origin app and
  returns its read tools (``*_get``, lists, status/overview, search) so they are
  offered in the turn even when tool retrieval would not pick them.

Both are pure helpers: they never call a tool and never fail a turn.
"""

from __future__ import annotations

import json
import re
import time
import unicodedata
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Set

#: Read tools offered up front for the origin app (the rest stay reachable
#: through ``lookup_tools``).
ORIGIN_TOOL_LIMIT = 12

_READ = {"get": 0, "show": 0, "detail": 0, "view": 0, "status": 1, "overview": 1, "summary": 1, "dashboard": 1,
         "list": 2, "search": 2, "find": 2, "query": 2}
_WRITE = {"set", "update", "patch", "delete", "remove", "create", "add", "import", "apply", "run", "start", "stop",
          "send", "save", "write", "edit", "move", "rename", "mark", "archive", "clear", "reset", "upload"}


def _norm(text: Any) -> str:
    """'Kafka's Hoard' -> 'kafka'; 'phileas_hoard' -> 'phileas'; 'Dürer's Hoard' -> 'durer'."""
    value = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode("ascii").lower()
    value = re.sub(r"['’]s\b", "", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    words = [w for w in value.split() if w not in {"hoard", "the", "s"}]
    return "".join(words)


def origin_of(context: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    origin = (context or {}).get("origin") if isinstance(context, Mapping) else None
    return dict(origin) if isinstance(origin, Mapping) else {}


def _family(sid: str, name: Any, keys: Set[str]) -> bool:
    """The server is that app, or another instance of it ("Tantalus QA", "tantalus-b")."""
    return any(_norm(value) == key or _norm(value).startswith(key) for value in (sid, name) for key in keys)


def find_origin_server(servers: Mapping[str, Any], context: Optional[Mapping[str, Any]],
                       spaces: Optional[Callable[[Iterable[str]], Mapping[str, str]]] = None) -> Optional[str]:
    """``servers`` maps server_id -> display name. Returns the id of the origin
    app's server, matched by id or by name, or None when the app is not
    connected (or the match is ambiguous).

    When several connected servers are instances of the same app (a QA copy next
    to the everyday one, two profiles), their tools have the same names; the one
    serving the panel is the one whose data space is the panel's. ``spaces`` maps
    candidate server ids to the ``data_space_id`` their app reports."""
    origin = origin_of(context)
    keys = {_norm(origin.get("tool_server_id")), _norm(origin.get("app_id")), _norm(origin.get("app_name"))} - {""}
    if not keys:
        return None
    family = [sid for sid, name in servers.items() if _family(sid, name, keys)]
    if len(family) == 1:
        return family[0]
    if len(family) > 1:
        space = str(origin.get("data_space_id") or "").strip()
        if space and spaces is not None:
            try:
                known = spaces(family) or {}
            except Exception:  # noqa: BLE001 - an unreachable app only means no match
                known = {}
            same = [sid for sid in family if known.get(sid) == space]
            if len(same) == 1:
                return same[0]
        return None
    return None


#: server_id -> (data_space_id or "", checked_at)
_SPACE_CACHE: Dict[str, tuple] = {}
_SPACE_TTL_S = 600.0


def _app_urls(env: Mapping[str, Any]) -> list:
    urls = []
    for key, value in (env or {}).items():
        text = str(value or "").strip()
        if str(key).upper().endswith("_URL") and "OPENAPI" not in str(key).upper() and text.startswith(("http://", "https://")):
            urls.append(text.rstrip("/"))
    return urls


def server_data_spaces(server_ids: Iterable[str], env_of: Optional[Callable[[str], Mapping[str, Any]]] = None,
                       fetch: Optional[Callable[[str], Optional[Mapping[str, Any]]]] = None) -> Dict[str, str]:
    """``data_space_id`` of the app behind each MCP server, read from the app's own
    ``/api/copilot/info`` at the URL in the server's environment. Cached for a few
    minutes; a server whose app does not answer simply has no entry."""
    env_of = env_of or _server_env
    fetch = fetch or _fetch_info
    out: Dict[str, str] = {}
    now = time.monotonic()
    for sid in server_ids:
        cached = _SPACE_CACHE.get(sid)
        if cached and now - cached[1] < _SPACE_TTL_S:
            if cached[0]:
                out[sid] = cached[0]
            continue
        space = ""
        for url in _app_urls(env_of(sid) or {}):
            info = fetch(url + "/api/copilot/info")
            if isinstance(info, Mapping) and info.get("data_space_id"):
                space = str(info["data_space_id"])
                break
        _SPACE_CACHE[sid] = (space, now)
        if space:
            out[sid] = space
    return out


def _server_env(server_id: str) -> Mapping[str, Any]:
    try:
        from core.database import McpServer, SessionLocal
        with SessionLocal() as db:
            row = db.query(McpServer).filter(McpServer.id == server_id).first()
            return json.loads(row.env) if row is not None and row.env else {}
    except Exception:  # noqa: BLE001
        return {}


def _fetch_info(url: str) -> Optional[Mapping[str, Any]]:
    try:
        import httpx
        response = httpx.get(url, timeout=0.8, trust_env=False)
        return response.json() if response.status_code == 200 else None
    except Exception:  # noqa: BLE001
        return None


def _tokens(tool_name: str) -> list:
    return [t for t in re.split(r"[^a-z0-9]+", str(tool_name).lower()) if t]


def _rank(tool_name: str) -> Optional[int]:
    """Read rank of a tool name, or None when it is not a plain read
    (any write verb in the name wins: ``comment_set_status`` is a write)."""
    tokens = _tokens(tool_name)
    if any(t in _WRITE for t in tokens):
        return None
    ranks = [_READ[t] for t in tokens if t in _READ]
    return min(ranks) if ranks else None


def _stems(context: Optional[Mapping[str, Any]]) -> Set[str]:
    """Name stems of what is open: kind 'document' -> {'document', 'doc'}."""
    out: Set[str] = set()
    objects = (context or {}).get("active_objects") if isinstance(context, Mapping) else None
    for obj in objects or []:
        kind = _norm(obj.get("kind")) if isinstance(obj, Mapping) else ""
        if len(kind) >= 3:
            out |= {kind, kind[:3], kind.rstrip("s")}
    return out


def _relevance(tool_name: str, stems: Set[str], app: str) -> int:
    """0: about an open object's kind; 1: the app's own overview; 2: anything else."""
    first = (_tokens(tool_name) or [""])[0]
    if stems and any(first.startswith(st) or (len(first) >= 3 and st.startswith(first)) for st in stems):
        return 0
    if app and first.startswith(app):
        return 1
    return 2


def pick_read_tools(tool_names: Iterable[str], limit: int = ORIGIN_TOOL_LIMIT,
                    context: Optional[Mapping[str, Any]] = None) -> list:
    """The app's plain read tools, those about the open objects first, then the
    app's own status/overview, then the rest; capped at ``limit``."""
    stems = _stems(context)
    origin = origin_of(context)
    app = _norm(origin.get("tool_server_id") or origin.get("app_id") or origin.get("app_name"))
    ranked = sorted({n for n in tool_names if _rank(n) is not None},
                    key=lambda n: (_relevance(n, stems, app), _rank(n), len(n), n))
    return ranked[:max(0, int(limit))]


def origin_app_tools(manager: Any, context: Optional[Mapping[str, Any]], limit: int = ORIGIN_TOOL_LIMIT) -> Set[str]:
    """Qualified names (``mcp__<server>__<tool>``) of the origin app's read tools."""
    try:
        connections = getattr(manager, "_connections", {}) or {}
        tools = getattr(manager, "_tools", {}) or {}
        servers = {sid: (connections.get(sid) or {}).get("name", sid) for sid in tools}
        server = find_origin_server(servers, context, server_data_spaces)
        if not server:
            return set()
        names = [t.get("name") for t in tools.get(server) or [] if isinstance(t, Mapping) and t.get("name")]
        return {f"mcp__{server}__{name}" for name in pick_read_tools(names, limit, context)}
    except Exception:  # noqa: BLE001 - tool selection must never break a turn
        return set()


def origin_server_note(manager: Any, context: Optional[Mapping[str, Any]]) -> Optional[str]:
    """When the origin app is connected more than once (same tool names on several
    servers), name the server that serves this panel's data space."""
    try:
        connections = getattr(manager, "_connections", {}) or {}
        tools = getattr(manager, "_tools", {}) or {}
        servers = {sid: (connections.get(sid) or {}).get("name", sid) for sid in tools}
        origin = origin_of(context)
        keys = {_norm(origin.get("tool_server_id")), _norm(origin.get("app_id")), _norm(origin.get("app_name"))} - {""}
        family = [sid for sid, name in servers.items() if keys and _family(sid, name, keys)]
        if len(family) < 2:
            return None
        server = find_origin_server(servers, context, server_data_spaces)
        app = str(origin.get("app_name") or origin.get("app_id") or "").strip()
        if server:
            others = ", ".join(f"mcp__{sid}__*" for sid in family if sid != server)
            return (f"{app} is connected more than once with the same tool names. This panel's data is served by "
                    f"mcp__{server}__* ({servers.get(server, server)}); use those tools for it. "
                    f"{others} belong to other {app} data spaces.")
        return (f"{app} is connected more than once with the same tool names and it is not known which one serves "
                f"this panel; before changing anything, read with each server's status tool and use the one whose "
                f"data space is {origin.get('data_space_id') or 'the panel'}'s, or ask.")
    except Exception:  # noqa: BLE001 - a hint must never break a turn
        return None


def origin_hint_message(context: Optional[Mapping[str, Any]], manager: Any = None) -> Optional[Dict[str, str]]:
    origin = origin_of(context)
    app = str(origin.get("app_name") or origin.get("app_id") or "").strip()
    if not app:
        return None
    view = (context or {}).get("view") if isinstance(context, Mapping) else None
    has_objects = bool((context or {}).get("active_objects")) if isinstance(context, Mapping) else False
    lines = [
        f"The user is talking to you from the {app} panel, inside that application.",
        "Words like \"this\", \"here\", \"the open one\", \"what I have open\" or \"this document\" refer to the "
        "objects in the Hoard context (active_objects, view and any unsaved editor fields), not to desktop windows, "
        "browser tabs or other applications.",
        "Answer from that context when it is enough. To read more or to change something, use "
        f"{app}'s own tools (find them with lookup_tools if they are not listed). Do not inspect desktop windows "
        "or other apps unless the user asks for them.",
    ]
    if not has_objects:
        lines.append("Right now no specific object is open in the app" +
                     (f" (page: {view.get('page')})" if isinstance(view, Mapping) and view.get("page") else "") +
                     "; say so if the user asks what is open.")
    if manager is not None:
        note = origin_server_note(manager, context)
        if note:
            lines.append(note)
    return {"role": "system", "content": " ".join(lines)}


__all__ = ["ORIGIN_TOOL_LIMIT", "origin_of", "find_origin_server", "pick_read_tools", "server_data_spaces",
           "origin_app_tools", "origin_server_note", "origin_hint_message"]
