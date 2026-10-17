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

import contextvars
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
    if not family:
        return None
    space = str(origin.get("data_space_id") or "").strip()
    known: Mapping[str, str] = {}
    if space and spaces is not None:
        try:
            known = spaces(family) or {}
        except Exception:  # noqa: BLE001 - an unreachable app only means no match
            known = {}
    if len(family) == 1:
        # The one connected instance serves another data space (a QA panel next to the everyday app's tools):
        # its tools are not this panel's.
        if known.get(family[0]) and known[family[0]] != space:
            return None
        return family[0]
    same = [sid for sid in family if known.get(sid) == space]
    return same[0] if len(same) == 1 else None


#: The MCP server of the Hoard a panel turn comes from, for the rest of that turn
#: (``lookup_tools`` lists its matches first). Task-local like the turn itself.
_ORIGIN_SERVER: contextvars.ContextVar = contextvars.ContextVar("hoard_origin_server", default="")


def current_origin_server() -> str:
    return _ORIGIN_SERVER.get() or ""


def remember_origin_server(manager: Any, context: Optional[Mapping[str, Any]]) -> str:
    """Resolve and keep the origin server for this turn; '' when unknown."""
    try:
        connections = getattr(manager, "_connections", {}) or {}
        tools = getattr(manager, "_tools", {}) or {}
        servers = {sid: (connections.get(sid) or {}).get("name", sid) for sid in tools}
        server = find_origin_server(servers, context, server_data_spaces) or ""
    except Exception:  # noqa: BLE001
        server = ""
    _ORIGIN_SERVER.set(server)
    return server


#: preset id -> monotonic time of the last automatic connection attempt.
_CONNECT_TRIED: Dict[str, float] = {}
#: One automatic attempt per app every this many seconds (an app that is off
#: is not probed again on every turn of its panel).
CONNECT_RETRY_S = 300.0


def _connected_family(manager: Any, keys: Set[str]) -> list:
    connections = getattr(manager, "_connections", {}) or {}
    tools = getattr(manager, "_tools", {}) or {}
    return [sid for sid in tools if _family(sid, (connections.get(sid) or {}).get("name", sid), keys)]


def origin_preset(context: Optional[Mapping[str, Any]], presets: Optional[Iterable[Mapping[str, Any]]] = None) -> str:
    """Id of the connector preset (an installed plugin manifest) of the panel's
    app: by app id or tool server id, else by name; '' when there is none."""
    origin = origin_of(context)
    keys = {_norm(origin.get("tool_server_id")), _norm(origin.get("app_id")), _norm(origin.get("app_name"))} - {""}
    if not keys:
        return ""
    if presets is None:
        try:
            from src import connectors
            presets = connectors.list_presets()
        except Exception:  # noqa: BLE001
            return ""
    rows = [p for p in presets or [] if isinstance(p, Mapping) and p.get("id")]
    for p in rows:
        if _norm(p["id"]) in keys:
            return str(p["id"])
    for p in rows:
        if _norm(p.get("name")) in keys:
            return str(p["id"])
    return ""


async def _internal_post(path: str, body: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    """POST to Faustus's own API as the in-process tool layer (admin routes)."""
    import httpx
    from core.constants import internal_api_base
    from core.middleware import INTERNAL_TOOL_HEADER, INTERNAL_TOOL_TOKEN
    async with httpx.AsyncClient(timeout=90.0, trust_env=False) as client:
        response = await client.post(internal_api_base() + path, json=dict(body),
                                     headers={INTERNAL_TOOL_HEADER: INTERNAL_TOOL_TOKEN})
        return response.json() if response.status_code == 200 else None


async def connect_origin_app(manager: Any, context: Optional[Mapping[str, Any]], *,
                             post: Optional[Callable[..., Any]] = None, find: Optional[Callable[..., Any]] = None,
                             connectors_of: Optional[Callable[[str], list]] = None,
                             presets: Optional[Iterable[Mapping[str, Any]]] = None,
                             clock: Callable[[], float] = time.monotonic) -> str:
    """A panel turn from a Hoard whose tools are not connected to Faustus although
    its plugin manifest is installed: connect it now, the way the Connectors
    screen does (switch its connector back on, or adopt the running app), so
    the turn gets the app's tools.

    Live 17-10-2026: Silver's Hoard had a manifest but no connector, and its
    copilot answered with nothing but web searches. Returns "connected",
    "adopted" or "" (already connected, no manifest, app not running, or tried a
    moment ago). Never raises: a turn always goes ahead."""
    try:
        origin = origin_of(context)
        keys = {_norm(origin.get("tool_server_id")), _norm(origin.get("app_id")), _norm(origin.get("app_name"))} - {""}
        if not keys or _connected_family(manager, keys):
            return ""
        preset_id = origin_preset(context, presets)
        if not preset_id:
            return ""
        now = clock()
        if now - _CONNECT_TRIED.get(preset_id, float("-inf")) < CONNECT_RETRY_S:
            return ""
        _CONNECT_TRIED[preset_id] = now
        post = post or _internal_post
        if connectors_of is None:
            def connectors_of(pid: str) -> list:
                from src import connector_sidecar
                return [e for e in connector_sidecar.list_connectors(redact=True) if e.get("preset_id") == pid]
        existing = connectors_of(preset_id)
        if existing:
            result = await post(f"/api/app-connectors/{existing[0]['id']}/connect", {})
            return "connected" if isinstance(result, Mapping) and result.get("connected") else ""
        if find is None:
            from src import connector_discovery, connectors
            preset = connectors.get_preset(preset_id)
            if preset is None:
                return ""
            candidate = await connector_discovery.find_app(preset)
        else:
            candidate = await find(preset_id)
        port = int(getattr(candidate, "port", 0) or 0) if candidate is not None else 0
        if not port:
            return ""
        result = await post("/api/app-connectors/adopt", {"port": port, "preset_id": preset_id})
        return "adopted" if isinstance(result, Mapping) and result.get("id") else ""
    except Exception:  # noqa: BLE001 - connecting is a convenience; the turn goes on without it
        import logging
        logging.getLogger(__name__).debug("[hoard-copilot] connecting the origin app failed", exc_info=True)
        return ""


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


def _refs_from(tool: str, args: Any, result: Mapping[str, Any]) -> list:
    """Typed references a tool call names: ``*_id`` arguments, and the object a creation returns."""
    refs, seen = [], set()

    def add(kind: str, object_id: Any, title: Any = "") -> None:
        oid = str(object_id or "").strip()
        if oid and oid not in seen and len(oid) <= 200:
            seen.add(oid)
            refs.append({"kind": kind, "object_id": oid, "title": str(title or "")[:200]})

    first = (_tokens(tool) or [""])[0]
    try:
        parsed = json.loads(args) if isinstance(args, str) and args.strip().startswith("{") else args
    except ValueError:
        parsed = None
    if isinstance(parsed, Mapping):
        for key, value in parsed.items():
            if str(key).endswith("_id") and isinstance(value, (str, int)):
                add(str(key)[:-3], value, parsed.get("title") or parsed.get("name"))
    output = result.get("output")
    try:
        body = json.loads(output or "") if isinstance(output, str) else output
    except ValueError:
        body = None
    if isinstance(body, Mapping) and isinstance(body.get("result"), Mapping):
        body = body["result"]
    if isinstance(body, Mapping):
        candidates = [(first, body)] + [(str(k), v) for k, v in body.items() if isinstance(v, Mapping)]
        for kind, item in candidates:
            if item.get("id") and (item.get("title") or item.get("name")):
                add(kind, item.get("id"), item.get("title") or item.get("name"))
    elif isinstance(output, str):
        # A long result arrives cut for the model; its head still names the object.
        head = re.match(r'\s*\{\s*"id"\s*:\s*"([^"]{1,200})"', output)
        title = re.search(r'"(?:title|name)"\s*:\s*"([^"]{1,200})"', output[:2000])
        if head:
            add(first, head.group(1), title.group(1) if title else "")
    if isinstance(output, str):
        # Typed references the Hoards print (hoard://<app>/<kind>/<id>).
        for match in re.finditer(r"hoard://[a-z0-9_-]+/([a-z0-9_-]+)/([A-Za-z0-9_.:-]{1,200})", output[:20000]):
            add(match.group(1), match.group(2))
    return refs[:8]


def note_participation(manager: Any, session_id: str, tool_name: str, args: Any, result: Mapping[str, Any]) -> bool:
    """After a successful MCP call in a Hoard conversation, keep which other app took part and with what."""
    try:
        name = str(tool_name or "")
        if not session_id or not name.startswith("mcp__") or (result or {}).get("exit_code") not in (0, None):
            return False
        _, server, tool = name.split("__", 2)
        connections = getattr(manager, "_connections", {}) or {}
        app = _norm((connections.get(server) or {}).get("name") or server)
        if not app:
            return False
        effect = "read" if _rank(tool) is not None else ("write" if any(t in _WRITE for t in _tokens(tool)) else "read")
        from services.hoard_copilot import get_store
        return get_store().record_participation(session_id, app, server, _refs_from(tool, args, result or {}), effect, tool)
    except Exception:  # noqa: BLE001 - bookkeeping must never break a turn
        return False


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
        app = str(origin.get("app_name") or origin.get("app_id") or "").strip()
        if not family:
            if not keys:
                return None
            # Live 17-10: with no Silver's Hoard server the copilot spent nine
            # minutes looking for Smogon sets on the web instead of saying so.
            return (f"{app} is not connected to Faustus as a tool server right now, so none of its tools can read or "
                    f"change anything in this turn. Say so plainly and that it is connected from Connectors (or by "
                    f"reopening the panel once the app is running); do not stand in for its tools with web searches "
                    f"or other apps, and do not claim any change was made in {app}.")
        server = find_origin_server(servers, context, server_data_spaces)
        if len(family) == 1:
            if server:
                return None
            return (f"The {app} tools connected to Faustus (mcp__{family[0]}__*) serve another {app} data space, not this "
                    f"panel's ({origin.get('data_space_id')}). Do not use them to read or change the objects of this "
                    f"panel; say that this {app} is not connected to Faustus as a tool server.")
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
        "For a requested change, call the tools directly, carry out the change and verify the result before the final "
        "reply. Announcing a call or a retry is not executing it; do not end with a promise of what you will do next.",
    ]
    if has_objects:
        refs = [{key: item[key] for key in ("kind", "object_id") if key in item}
                for item in (context or {}).get("active_objects", []) if isinstance(item, Mapping) and item.get("object_id")]
        lines.append("Active object references for this turn (literal tool ID arguments): " + json.dumps(refs, ensure_ascii=False) +
                     ". Copy each complete object_id character for character into the corresponding tool argument; "
                     "never shorten or reconstruct it. A not_found result for a different id does not mean the active "
                     "object is unavailable: compare the argument with these references and retry with the exact id.")
        lines.append("The context objects are identified by their ids, not their titles. If one of them can no longer be read "
                     "(deleted or unavailable), say that it is no longer available and do not act on another object because it "
                     "has the same title or name; ask which one the user means before changing anything else.")
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
           "origin_app_tools", "origin_server_note", "origin_hint_message", "current_origin_server",
           "remember_origin_server", "note_participation", "connect_origin_app", "origin_preset", "CONNECT_RETRY_S"]
