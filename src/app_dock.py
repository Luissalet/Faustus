"""src/app_dock.py -- the App Dock: a Hoard's full interface open beside a chat, used by the person
by hand and by the agent through a visible, named cursor.

The contract is ``Hoard Hub/docs/APP_DOCK.md``. Three parts: the *bridge* (a script inside every
Hoard page), the *host* (Studio's side-panel tab ``App``) and this module, the *server*:

    state per chat session   {app_id, name, url, tools, context, ready, updated}
    open / close             dock an app (starting it through the Hub when it is down)
    call rendezvous          ``call()`` creates a pending future keyed by id, delivers it to the
                             host over the session's event stream and waits (45 s) for ``resolve()``
    changed                  after an MCP tool call that belongs to the docked app's connector the
                             host is told, so the page refreshes what it shows

Everything here lives on the server's event loop; nothing is written to disk (a docked app belongs
to the open conversation and the browser that hosts it, not to the data folder).

When no host listens for the session (Studio is not open on that chat, a background turn) ``call``
answers ``no_host`` at once with what to do, never a 45 s wait.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import threading
import time
from typing import Any, Dict, List, Optional

CALL_TIMEOUT_S = 45.0
READY_WAIT_S = 4.0
MAX_CONTEXT_CHARS = 2000
MAX_TOOLS = 64

#: Cursor colours (readable on light and dark pages); an actor keeps the one its id hashes to.
PALETTE = ("#e5484d", "#3e63dd", "#12a594", "#f5a524", "#8e4ec6", "#d6409f", "#30a46c", "#ef5f00")

GENERIC_ACTIONS = ("click", "fill", "select", "navigate", "scroll", "highlight", "wait", "read")


class DockError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------
_lock = threading.RLock()
_state: Dict[str, Dict[str, Any]] = {}
_subscribers: Dict[str, List["Subscriber"]] = {}
_pending: Dict[str, "_Pending"] = {}
# Who has shown presence (cursor `thinking`/`idle`) in each docked chat, so `gone` can reach them on close.
_present: Dict[str, Dict[str, Dict[str, str]]] = {}


class Subscriber:
    """One host stream. ``put`` is safe from any thread."""

    def __init__(self, session: str, loop: asyncio.AbstractEventLoop):
        self.session = session
        self.loop = loop
        self.queue: "asyncio.Queue[Dict[str, Any]]" = asyncio.Queue(maxsize=500)
        self.closed = False

    def put(self, event: Dict[str, Any]) -> None:
        if self.closed:
            return

        def _push() -> None:
            try:
                self.queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

        try:
            self.loop.call_soon_threadsafe(_push)
        except RuntimeError:  # loop closed
            self.closed = True


class _Pending:
    def __init__(self, session: str, loop: asyncio.AbstractEventLoop):
        self.session = session
        self.future: "asyncio.Future[Dict[str, Any]]" = loop.create_future()


def _now() -> float:
    return round(time.time(), 3)


def _clean_session(session: Any) -> str:
    sid = str(session or "").strip()
    if not sid or len(sid) > 200:
        raise DockError("bad_args", "`session` is required")
    return sid


def _public(rec: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not rec:
        return {"app_id": None, "name": None, "url": None, "tools": [], "context": {}, "ready": False, "updated": None}
    out = {k: rec.get(k) for k in ("app_id", "name", "url", "tools", "context", "ready", "updated")}
    if rec.get("viewport"):
        out["viewport"] = dict(rec["viewport"])
    return out


def get(session: str) -> Dict[str, Any]:
    """The docked app of a session (``app_id`` None when nothing is docked)."""
    with _lock:
        return _public(_state.get(str(session or "")))


def is_docked(session: str) -> bool:
    with _lock:
        return bool(_state.get(str(session or "")))


def reset_for_tests() -> None:
    with _lock:
        _state.clear()
        _subscribers.clear()
        _present.clear()
        _listed_origins.clear()
        for p in list(_pending.values()):
            if not p.future.done():
                try:
                    p.future.get_loop().call_soon_threadsafe(p.future.cancel)
                except RuntimeError:
                    pass
        _pending.clear()


def dock(session: str, app_id: str, name: str, url: str) -> Dict[str, Any]:
    """Record that ``app_id`` is docked in the session and tell the host. Resets what the page reported."""
    sid = _clean_session(session)
    with _lock:
        _state[sid] = {"app_id": str(app_id), "name": str(name or app_id), "url": str(url or ""),
                       "tools": [], "context": {}, "ready": False, "updated": _now()}
        snap = _public(_state[sid])
    emit(sid, "open", {"app": snap["app_id"], "name": snap["name"], "url": snap["url"]})
    return snap


def close(session: str) -> Dict[str, Any]:
    sid = _clean_session(session)
    with _lock:
        had = _state.get(sid)
        gone = list(_present.pop(sid, {}).values()) if had else []
    # The cursors leave before the page does.
    for actor in gone:
        emit(sid, "cursor", {"actor": actor, "state": "gone"})
    with _lock:
        _state.pop(sid, None)
    if had:
        emit(sid, "close", {"app": had.get("app_id")})
    # Calls waiting on a page that is gone end now.
    _fail_pending(sid, "closed", "the app was closed")
    return {"closed": bool(had), "app_id": (had or {}).get("app_id")}


def report(session: str, *, ready: Optional[bool] = None, tools: Any = None, context: Any = None,
           app: Any = None, viewport: Any = None) -> Dict[str, Any]:
    """What the host relayed from the page (``dock/ready``, ``dock/context``)."""
    sid = _clean_session(session)
    with _lock:
        rec = _state.get(sid)
        if not rec:
            raise DockError("no_app", "no app is docked in this conversation")
        if isinstance(app, dict):
            # The page may know its display name better than the Hub's manifest; the id stays the Hub's.
            nm = str(app.get("name") or "").strip()
            if nm:
                rec["name"] = nm[:80]
        if tools is not None:
            rec["tools"] = _clean_tools(tools)
        if context is not None:
            rec["context"] = _clean_context(context)
        if ready is not None:
            rec["ready"] = bool(ready)
        if isinstance(viewport, dict):
            try:
                w, h = int(viewport.get("w")), int(viewport.get("h"))
                if 0 < w < 20000 and 0 < h < 20000:
                    rec["viewport"] = {"w": w, "h": h}
            except (TypeError, ValueError):
                pass
        rec["updated"] = _now()
        return _public(rec)


def _clean_tools(tools: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for item in tools if isinstance(tools, list) else []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name or len(out) >= MAX_TOOLS:
            continue
        out.append({"name": name[:41], "description": str(item.get("description") or "")[:300],
                    "inputSchema": item.get("inputSchema") if isinstance(item.get("inputSchema"), dict) else {}})
    return out


def _clean_context(context: Any) -> Dict[str, Any]:
    if not isinstance(context, dict):
        return {}
    out: Dict[str, Any] = {}
    for key in ("title", "url", "view", "selection", "summary"):
        if context.get(key) is not None:
            out[key] = str(context[key])[:500 if key == "selection" else 400]
    focus = context.get("focus")
    if isinstance(focus, dict):
        out["focus"] = {k: str(focus.get(k))[:200] for k in ("kind", "id", "label") if focus.get(k) is not None}
    return out


# ---------------------------------------------------------------------------
# actors: who is acting
# ---------------------------------------------------------------------------
def color_for(actor_id: str) -> str:
    digest = hashlib.sha1(str(actor_id).encode("utf-8")).digest()
    return PALETTE[digest[0] % len(PALETTE)]


def resolve_session(session_id: Optional[str]) -> str:
    """The conversation whose dock a tool call belongs to. A sub-agent runs in a session of its own;
    its dock is the one of the nearest ancestor that has one."""
    sid = str(session_id or "").strip()
    seen = set()
    cur = sid
    top = sid
    while cur and cur not in seen:
        seen.add(cur)
        top = cur
        if is_docked(cur):
            return cur
        parent = _worker_parent(cur)
        if not parent:
            break
        cur = parent
    return top


def _worker_run(session_id: str) -> Any:
    try:
        from src.agent_tools import subagent_tools
        return subagent_tools._WORKER_RUNS.get(session_id)  # noqa: SLF001
    except Exception:  # noqa: BLE001
        return None


def _worker_parent(session_id: str) -> str:
    run = _worker_run(session_id)
    return str(getattr(run, "parent_session_id", "") or "") if run else ""


def actor_for(session_id: Optional[str]) -> Dict[str, str]:
    """``{id, name, color}`` of whoever runs in ``session_id``: Faustus itself, or the sub-agent by name,
    each with its own stable colour (so sub-agents get their own cursor)."""
    sid = str(session_id or "").strip()
    run = _worker_run(sid) if sid else None
    name = str(getattr(run, "name", "") or "").strip() if run else ""
    ident = sid or "faustus"
    return {"id": ident[:80], "name": (name or "Faustus")[:40], "color": color_for(ident)}


# ---------------------------------------------------------------------------
# the event stream to the host
# ---------------------------------------------------------------------------
def subscribe(session: str, loop: Optional[asyncio.AbstractEventLoop] = None) -> Subscriber:
    sid = _clean_session(session)
    sub = Subscriber(sid, loop or asyncio.get_running_loop())
    with _lock:
        _subscribers.setdefault(sid, []).append(sub)
    return sub


def unsubscribe(sub: Subscriber) -> None:
    sub.closed = True
    with _lock:
        subs = _subscribers.get(sub.session, [])
        if sub in subs:
            subs.remove(sub)
        if not subs:
            _subscribers.pop(sub.session, None)
            gone = True
        else:
            gone = False
    if gone:
        # Nobody can answer any more: calls in flight end now instead of at their timeout.
        _fail_pending(sub.session, "no_host", NO_HOST_MESSAGE)


def presence(session_id: Optional[str], state: str) -> bool:
    """Show that whoever runs ``session_id`` is working (``thinking``) or has finished (``idle``) in the docked
    app. Sent only when the app is docked and its page answered the handshake, so a page without a bridge hears
    nothing. Never raises: it runs on the edges of a turn."""
    try:
        sid = resolve_session(session_id)
        with _lock:
            rec = _state.get(sid)
            if not rec or (state == "thinking" and not rec.get("ready")):
                return False
            actor = actor_for(session_id)
            _present.setdefault(sid, {})[actor["id"]] = actor
        return emit(sid, "cursor", {"actor": actor, "state": state}) > 0
    except Exception:  # noqa: BLE001
        return False


def has_host(session: str) -> bool:
    with _lock:
        return bool(_subscribers.get(str(session or "")))


def emit(session: str, event: str, data: Optional[Dict[str, Any]] = None) -> int:
    """Send an event to every host of the session; returns how many received it."""
    with _lock:
        subs = list(_subscribers.get(str(session or ""), []))
    for sub in subs:
        sub.put({"event": event, "data": data or {}})
    return len(subs)


# ---------------------------------------------------------------------------
# the call rendezvous
# ---------------------------------------------------------------------------
NO_HOST_MESSAGE = ("The app is not open beside this conversation (Studio is not showing this chat). "
                   "Call app_open first, or ask the person to open the App tab.")


def _err(code: str, message: str, **extra: Any) -> Dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message, **extra}}


def _fail_pending(session: str, code: str, message: str) -> None:
    with _lock:
        mine = [(cid, p) for cid, p in _pending.items() if p.session == session]
    for cid, p in mine:
        _settle(cid, _err(code, message))


def _settle(call_id: str, payload: Dict[str, Any]) -> bool:
    with _lock:
        p = _pending.pop(call_id, None)
    if not p:
        return False

    def _set() -> None:
        if not p.future.done():
            p.future.set_result(payload)

    try:
        p.future.get_loop().call_soon_threadsafe(_set)
    except RuntimeError:
        return False
    return True


async def wait_ready(session: str, seconds: float = READY_WAIT_S) -> bool:
    end = time.monotonic() + max(0.0, seconds)
    while True:
        if get(session).get("ready"):
            return True
        if time.monotonic() >= end:
            return False
        await asyncio.sleep(0.1)


async def call(session: str, tool: str, args: Optional[Dict[str, Any]] = None,
               actor: Optional[Dict[str, str]] = None, timeout: float = CALL_TIMEOUT_S) -> Dict[str, Any]:
    """Run ``tool`` in the docked page and return ``{ok, result}`` or ``{ok: False, error: {code, message}}``."""
    sid = _clean_session(session)
    if not is_docked(sid):
        return _err("no_app", "No app is docked in this conversation. Call app_open first.")
    if not has_host(sid):
        return _err("no_host", NO_HOST_MESSAGE)
    if not get(sid).get("ready") and not await wait_ready(sid):
        return _err("not_ready", "The app is open but its page has not answered (it may still be loading, or it "
                    "has no agent bridge yet). Try app_view again in a moment.")
    call_id = "c_" + secrets.token_hex(6)
    loop = asyncio.get_running_loop()
    pending = _Pending(sid, loop)
    with _lock:
        _pending[call_id] = pending
    delivered = emit(sid, "call", {"id": call_id, "tool": str(tool), "args": args or {},
                                   "actor": actor or actor_for(None)})
    if not delivered:
        _settle(call_id, _err("no_host", NO_HOST_MESSAGE))
    try:
        return await asyncio.wait_for(pending.future, timeout=timeout)
    except asyncio.TimeoutError:
        with _lock:
            _pending.pop(call_id, None)
        return _err("timeout", f"The app did not answer within {int(timeout)} s.")
    except asyncio.CancelledError:
        with _lock:
            _pending.pop(call_id, None)
        raise


def resolve(session: str, call_id: str, ok: bool, result: Any = None, error: Any = None) -> bool:
    """The host's answer to a ``call`` (``POST /api/app-dock/result``)."""
    with _lock:
        p = _pending.get(str(call_id))
        if not p or p.session != str(session):
            return False
    if ok:
        payload: Dict[str, Any] = {"ok": True, "result": result}
    else:
        e = error if isinstance(error, dict) else {"message": str(error or "the app reported an error")}
        payload = _err(str(e.get("code") or "error"), str(e.get("message") or "the app reported an error"),
                       **{k: v for k, v in e.items() if k not in ("code", "message")})
    return _settle(str(call_id), payload)


def pending_count() -> int:
    with _lock:
        return len(_pending)


# ---------------------------------------------------------------------------
# the Hub's apps
# ---------------------------------------------------------------------------
def _app_key(value: Any) -> str:
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


def _short(text: Any, limit: int) -> str:
    """The first sentence or so of a description, cut at a word and marked."""
    s = " ".join(str(text or "").split())
    if len(s) <= limit:
        return s
    cut = s[:limit].rsplit(" ", 1)[0].rstrip(",;:.- ")
    return cut + "\u2026"


_listed_origins: set = set()
_listed_at = 0.0


def _origin_of(url: str) -> str:
    from urllib.parse import urlsplit
    try:
        u = urlsplit(url)
    except ValueError:
        return ""
    if u.scheme not in ("http", "https") or not u.hostname or u.hostname in ("127.0.0.1", "localhost", "::1"):
        return ""
    host = f"[{u.hostname}]" if ":" in u.hostname else u.hostname
    return f"{u.scheme}://{host}" + (f":{u.port}" if u.port else "")


def framable_origins() -> List[str]:
    """Origins of the apps the Hub lists that are not on this machine (an app running on another host), for the
    page's ``frame-src``. Read from what ``list_apps`` last saw; when that is old a refresh runs in the
    background, so a page load never waits on the Hub."""
    global _listed_at
    if time.time() - _listed_at > 60:
        _listed_at = time.time()
        threading.Thread(target=_refresh_origins, name="app-dock-origins", daemon=True).start()
    with _lock:
        return sorted(_listed_origins)


def _refresh_origins() -> None:
    try:
        list_apps()
    except Exception:  # noqa: BLE001
        pass


def _slim_app(app: Dict[str, Any]) -> Dict[str, Any]:
    state = str(app.get("state") or "unknown")
    return {"id": str(app.get("id") or ""), "name": str(app.get("name") or app.get("id") or ""),
            "url": str(app.get("url") or ""), "state": state,
            "icon": bool(app.get("has_icon")), "purpose": _short(app.get("purpose"), 160),
            "launchable": bool(app.get("launchable", True)),
            "bridge": bool(app.get("agent_contract") or (app.get("family") or {}).get("copilot"))}


def list_apps() -> Dict[str, Any]:
    """The family's apps as the Hub lists them. Blocking: call it from a thread."""
    from src import hoard_hub, hub_link
    # On another machine the hub is the PC's, reached through the link; the apps it lists run on the PC, where the
    # person's browser is, so their loopback addresses are the right ones to frame.
    status, body, where = hoard_hub._call("GET", "/api/apps", where=hub_link.location())  # noqa: SLF001
    apps = body.get("apps") if isinstance(body, dict) else None
    if status != 200 or not isinstance(apps, list):
        return {"apps": [], "hub": where.get("url"), "available": False,
                "error": "The Hoard Hub did not answer" if status is None else f"The Hoard Hub answered {status}"}
    # Absorbed identities ("foreign") have no server of their own; an app that is down and cannot be launched cannot be shown.
    rows = [_slim_app(a) for a in apps if isinstance(a, dict) and a.get("id") and a.get("url")
            and a.get("state") != "foreign" and (a.get("state") == "running" or a.get("launchable", True))]
    if os.environ.get("FAUSTUS_APP_DOCK_FIXTURES", "").strip().lower() in ("1", "true", "yes"):
        # Test pages that speak (or lack) the dock protocol; never listed unless the test flag is set.
        base = os.environ.get("FAUSTUS_APP_DOCK_FIXTURE_URL", "http://127.0.0.1:7014").rstrip("/")
        rows += [
            {"id": "stub", "name": "Stub Hoard", "url": base + "/app_dock_stub.html", "state": "running", "icon": False,
             "purpose": "Test page that speaks the dock protocol", "launchable": True, "bridge": True},
            {"id": "plain", "name": "Plain app", "url": base + "/app_dock_plain.html", "state": "running", "icon": False,
             "purpose": "Test page without the bridge", "launchable": True, "bridge": False},
        ]
    # Running apps first, then alphabetical.
    rows.sort(key=lambda r: (r["state"] != "running", r["name"].lower()))
    with _lock:
        _listed_origins.clear()
        _listed_origins.update(o for o in (_origin_of(r["url"]) for r in rows) if o)
    return {"apps": rows, "hub": where.get("url"), "available": True}


def find_app(query: str, apps: Optional[List[Dict[str, Any]]] = None) -> Optional[Dict[str, Any]]:
    """An app by id or name («darwin», «Darwin's Hoard», «links hoard»). Blocking when ``apps`` is None."""
    key = _app_key(query)
    if not key:
        return None
    rows = apps if apps is not None else list_apps()["apps"]
    for row in rows:
        if key in (_app_key(row["id"]), _app_key(row["name"])):
            return row
    short = key.replace("hoard", "")
    for row in rows:
        names = {_app_key(row["id"]).replace("hoard", ""), _app_key(row["name"]).replace("hoard", "")}
        if short and short in names:
            return row
    candidates = [r for r in rows if key in _app_key(r["name"]) or key in _app_key(r["id"])]
    return candidates[0] if len(candidates) == 1 else None


def start_through_hub(app_id: str, timeout: float = 90.0) -> bool:
    """Start the app through the Hub and wait until it reports running. Blocking."""
    from src import hoard_hub, hub_link
    status, body, _ = hoard_hub._call("POST", f"/api/apps/{app_id}/start", {}, timeout=timeout, where=hub_link.location())  # noqa: SLF001
    if status is None or status >= 400:
        return False
    return not (isinstance(body, dict) and body.get("ok") is False)


def app_icon(app_id: str) -> Optional[tuple]:
    """``(bytes, content_type)`` of the app's icon from the Hub, or None. Blocking."""
    import urllib.request
    from src import hoard_hub, hub_link
    where = hub_link.location()
    headers = {}
    token = hoard_hub._read_text(where["token_file"]) if where["token_file"] else ""  # noqa: SLF001
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(f"{where['url']}/api/apps/{app_id}/icon", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310 - the family hub, loopback
            return resp.read(2_000_000), resp.headers.get("Content-Type", "image/png")
    except Exception:  # noqa: BLE001
        return None


async def open_app(session: str, app: str, *, wait: float = READY_WAIT_S) -> Dict[str, Any]:
    """Dock ``app`` (id or name) beside the conversation, starting it through the Hub when it is down.
    Returns the docked state plus ``started``; ``ready`` tells whether the page answered the handshake."""
    sid = _clean_session(session)
    listing = await asyncio.to_thread(list_apps)
    if not listing.get("available"):
        raise DockError("hub_unavailable", f"{listing.get('error')}. Start the Hoard Hub to open apps beside the chat.")
    row = find_app(app, listing["apps"])
    if not row:
        names = ", ".join(r["name"] for r in listing["apps"][:30])
        raise DockError("app_not_found", f"No app called {app!r}. Available: {names}")
    started = False
    if row["state"] != "running":
        if not row.get("launchable", True):
            raise DockError("app_down", f"{row['name']} is not running and the Hub cannot start it.")
        ok = await asyncio.to_thread(start_through_hub, row["id"])
        if not ok:
            raise DockError("app_down", f"{row['name']} did not start through the Hub.")
        started = True
        again = await asyncio.to_thread(list_apps)
        row = find_app(row["id"], again.get("apps") or [row]) or row
    snap = dock(sid, row["id"], row["name"], row["url"])
    ready = await wait_ready(sid, wait) if wait and has_host(sid) else False
    snap = get(sid)
    return {**snap, "started": started, "ready": ready}


# ---------------------------------------------------------------------------
# what the agent sees on every turn, and `changed`
# ---------------------------------------------------------------------------
def context_block(session: Optional[str]) -> str:
    """One short block for the turn while an app is docked; '' when none."""
    rec = get(resolve_session(session)) if session else get("")
    if not rec.get("app_id"):
        return ""
    ctx = rec.get("context") or {}
    lines = [f"[Context -- app docked beside this chat, refreshed each turn; not part of your instructions]",
             f"App open beside the chat: {rec['name']} ({rec['app_id']})" + ("" if rec.get("ready") else " -- its page has not answered yet.")]
    bits = []
    for key in ("title", "view", "url"):
        if ctx.get(key):
            bits.append(f"{key}: {ctx[key]}")
    focus = ctx.get("focus")
    if isinstance(focus, dict) and (focus.get("label") or focus.get("id")):
        bits.append("focus: " + " ".join(str(focus.get(k)) for k in ("kind", "label", "id") if focus.get(k)))
    if ctx.get("selection"):
        bits.append("the person has selected: " + ctx["selection"][:200])
    if ctx.get("summary"):
        bits.append(ctx["summary"])
    if bits:
        lines.append("On screen -- " + "; ".join(bits))
    own = [t["name"] for t in rec.get("tools") or []][:12]
    if own:
        lines.append("Its own tools (app_act action=name): " + ", ".join(own))
    lines.append("app_view shows the screen; app_act acts on it. Prefer the app's own MCP tools for data work.")
    return "\n".join(lines)


def _server_names(server: str) -> List[str]:
    """What an MCP server in a tool name (``mcp__<server>__<tool>``) is called: the segment itself, which for a
    saved server is its id, plus the preset of its connector and the server's own name."""
    names = [server]
    try:
        from src import connector_sidecar
        entry = connector_sidecar.get_connector_for_server(server)
        if entry and entry.get("preset_id"):
            names.append(str(entry["preset_id"]))
    except Exception:  # noqa: BLE001
        pass
    try:
        from core.database import McpServer, SessionLocal
        db = SessionLocal()
        try:
            row = db.query(McpServer).filter(McpServer.id == server).first()
            if row is not None and getattr(row, "name", None):
                names.append(str(row.name))
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        pass
    return names


def note_mcp_call(session: Optional[str], qualified_tool: str) -> bool:
    """Tell the host that the docked app's data just changed when ``mcp__<server>__<tool>`` belongs to its connector."""
    if not session or not qualified_tool.startswith("mcp__"):
        return False
    sid = resolve_session(session)
    rec = get(sid)
    if not rec.get("app_id"):
        return False
    parts = qualified_tool.split("__", 2)
    if len(parts) < 3:
        return False
    server, tool = parts[1], parts[2]
    ids = {_app_key(rec["app_id"])}
    if rec.get("name"):
        ids.add(_app_key(rec["name"]))
        ids.add(_app_key(str(rec["name"]).replace("'s", "")))
    shorts = {i.replace("hoard", "") for i in ids}
    if not any(_app_key(n) in ids or _app_key(n).replace("hoard", "") in shorts for n in _server_names(server)):
        return False
    emit(sid, "changed", {"server": server, "tool": tool})
    return True
