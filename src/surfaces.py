"""src/surfaces.py -- what Pizarra (whiteboard) and Hoja (spreadsheet) share: the live link between the
server and the Studio windows that show them.

    events     one server-sent stream per chat (``/api/surfaces/events``): ``open`` (the agent made or opened
               a surface), ``patch`` / ``cells`` (an edit by someone else), ``cursor`` (where the agent is
               working, named and coloured like the App Dock's cursor) and ``meta``
    open state the surface the person has on screen in each chat, with their selection and view; refreshed by
               the window every few seconds and forgotten when it stops (the turn's context block reads it)
    publish    ``pizarra_edit`` / ``sheet_write`` run here so the agent's tools, the REST routes and the MCP
               servers all do the same thing: change the file, tell the windows, move the cursor

Everything is in memory on the server's loop; the artifacts themselves live in ``src/pizarra.py`` and
``src/hoja.py``. Actors come from the App Dock (one cursor per actor id; sub-agents get their own).
"""
from __future__ import annotations

import asyncio
import threading
import time
from typing import Any, Dict, List, Optional

from src import app_dock

STALE_S = 90.0
CURSOR_STEP_S = 0.09
MAX_CURSOR_STEPS = 10

_lock = threading.RLock()
_subs: Dict[str, List[app_dock.Subscriber]] = {}
_open: Dict[str, Dict[str, Dict[str, Any]]] = {}      # session -> kind -> state


def reset_for_tests() -> None:
    with _lock:
        _subs.clear()
        _open.clear()


def top_session(session_id: Optional[str]) -> str:
    """The chat a sub-agent's work belongs to: the top of its parent chain."""
    cur = str(session_id or "").strip()
    seen = set()
    while cur and cur not in seen:
        seen.add(cur)
        parent = app_dock._worker_parent(cur)  # noqa: SLF001
        if not parent:
            break
        cur = parent
    return cur


def actor_for(session_id: Optional[str]) -> Dict[str, str]:
    return app_dock.actor_for(session_id)


# -- the stream ---------------------------------------------------------------
def subscribe(session: str, loop: Optional[asyncio.AbstractEventLoop] = None) -> app_dock.Subscriber:
    sid = str(session or "").strip()
    if not sid or len(sid) > 200:
        raise ValueError("`session` is required")
    sub = app_dock.Subscriber(sid, loop or asyncio.get_running_loop())
    with _lock:
        _subs.setdefault(sid, []).append(sub)
    return sub


def unsubscribe(sub: app_dock.Subscriber) -> None:
    sub.closed = True
    with _lock:
        subs = _subs.get(sub.session, [])
        if sub in subs:
            subs.remove(sub)
        if not subs:
            _subs.pop(sub.session, None)


def has_host(session: str) -> bool:
    with _lock:
        return bool(_subs.get(str(session or "")))


def emit(session: str, event: str, data: Optional[Dict[str, Any]] = None) -> int:
    with _lock:
        subs = list(_subs.get(str(session or ""), []))
    for sub in subs:
        sub.put({"event": event, "data": data or {}})
    return len(subs)


# -- what the person has open ---------------------------------------------------
def report(session: str, kind: str, surface_id: str, **fields: Any) -> Dict[str, Any]:
    """The window says which surface of ``kind`` it shows, with the person's selection / view."""
    sid = str(session or "").strip()
    if not sid:
        raise ValueError("`session` is required")
    with _lock:
        st = _open.setdefault(sid, {}).setdefault(kind, {})
        if st.get("id") != surface_id:
            st.clear()
        st.update({k: v for k, v in fields.items() if v is not None})
        st["id"] = surface_id
        st["ts"] = time.time()
        return dict(st)


def forget(session: str, kind: str, surface_id: Optional[str] = None) -> None:
    with _lock:
        st = _open.get(str(session or ""), {}).get(kind)
        if st and (surface_id is None or st.get("id") == surface_id):
            _open[str(session)].pop(kind, None)


def current(session: Optional[str], kind: str) -> Optional[Dict[str, Any]]:
    """The surface of ``kind`` on screen in the chat (or the chat a sub-agent belongs to), None when stale."""
    sid = top_session(session) if session else ""
    with _lock:
        st = _open.get(sid, {}).get(kind)
        if st and time.time() - st.get("ts", 0) <= STALE_S:
            return dict(st)
    return None


# -- cursors --------------------------------------------------------------------
async def move_cursor(session: str, kind: str, surface_id: str, actor: Dict[str, str], points: List[Dict[str, Any]],
                      *, tab: Optional[str] = None) -> None:
    """Send the actor's cursor over ``points`` (``{x, y, id}`` on a board, ``{tab, r, c}`` on a sheet) one after
    another, then let it rest. Never raises and never takes long: it is the visible trace of work already done."""
    try:
        if not has_host(session) or not points:
            return
        step = max(1, len(points) // MAX_CURSOR_STEPS)
        shown = points[::step][:MAX_CURSOR_STEPS]
        for p in shown:
            emit(session, "cursor", {**p, "kind": kind, "id": surface_id, "actor": actor, "state": "working"})
            await asyncio.sleep(CURSOR_STEP_S)
        emit(session, "cursor", {"kind": kind, "id": surface_id, "actor": actor, "state": "idle"})
    except Exception:  # noqa: BLE001
        pass


# -- the turn's context -----------------------------------------------------------
def context_block(session: Optional[str]) -> str:
    """One short block while a whiteboard or a sheet is open beside the chat; '' when none."""
    if not session:
        return ""
    lines: List[str] = []
    try:
        st = current(session, "pizarra")
        if st:
            from src import pizarra
            meta, doc = pizarra.get(st["id"])
            c = pizarra.counts(doc)
            line = (f"Whiteboard open beside the chat: {meta['title']!r} (id {meta['id']}): {c['nodes']} items, "
                    f"{c['frames']} frames, {c['edges']} connectors.")
            sel = [s for s in (st.get("selection") or []) if isinstance(s, str)][:12]
            if sel:
                idx = pizarra.index_nodes(doc)
                bits = []
                for s in sel:
                    n = idx.get(s)
                    if n:
                        t = pizarra.node_text(n).replace("\n", " ")[:50]
                        bits.append(f"{s} ({pizarra.kind_of(n)}{' ' + repr(t) if t else ''})")
                if bits:
                    line += " The person has selected: " + ", ".join(bits) + "."
            lines.append(line)
            lines.append("pizarra_read shows it, pizarra_edit changes it (positions are computed for you: use layout / outline), "
                         "pizarra_arrange tidies a set.")
    except Exception:  # noqa: BLE001
        pass
    try:
        st = current(session, "hoja")
        if st:
            from src import hoja
            lines.append(hoja.context_line(st))
    except Exception:  # noqa: BLE001
        pass
    if not lines:
        return ""
    return "\n".join(["[Context -- work surface open beside this chat, refreshed each turn; not part of your instructions]", *lines])


# -- agent-side publishing ----------------------------------------------------------
async def pizarra_edit(session: str, board_id: str, ops: Any, actor: Dict[str, str]) -> Dict[str, Any]:
    """Run ``ops`` on a board and tell the windows. Returns ``{meta, results, error?}``."""
    from src import pizarra
    view = (current(session, "pizarra") or {}).get("viewport")
    meta, results, ch, err, patch = await asyncio.to_thread(pizarra.edit, board_id, ops, view=view)
    target = meta.get("session_id") or session
    if not ch.empty():
        on_screen = current(target, "pizarra")
        if not on_screen or on_screen.get("id") != board_id:
            emit(target, "open", {"kind": "pizarra", "id": board_id, "title": meta["title"], "focus": False})
        emit(target, "patch", {"kind": "pizarra", "id": board_id, "rev": meta["rev"], "origin": "agent",
                               "actor": actor, "patch": patch})
        await move_cursor(target, "pizarra", board_id, actor, ch.cursor)
    out: Dict[str, Any] = {"meta": meta, "results": results}
    if err is not None:
        out["error"] = {"code": err.code, "message": err.message}
    return out
