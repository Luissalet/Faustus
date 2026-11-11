"""agent_tools/pizarra_tools.py -- the four Pizarra (whiteboard) tools (src/pizarra.py, src/surfaces.py).

    pizarra_create   a new board in this chat, optionally filled in the same call
    pizarra_read     compact outline: frames, items with id / kind / colour / box / text, connectors, the
                   person's selection and view
    pizarra_edit     a batch of ops (add, update, move, delete, connect, group, layout, align, order, outline)
    pizarra_arrange  tidy a set of items (all, the person's selection, a frame or a list) as grid / tree / flow /
                   timeline / mindmap

The board is a file on the server; the open window shows the agent's changes as they land, with the agent's
named cursor. Nothing here needs a window: without one the file is simply updated.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional

from src import pizarra, surfaces


def _args(content: Any) -> Dict[str, Any]:
    if isinstance(content, dict):
        return dict(content)
    raw = (content or "").strip() if isinstance(content, str) else ""
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}
    return {}


def _fail(tool: str, code: str, message: str) -> Dict[str, Any]:
    return {"error": f"{tool}: {message}", "error_class": "pizarra." + code, "exit_code": 1}


def _session(ctx: dict) -> str:
    return surfaces.top_session((ctx or {}).get("session_id"))


def resolve_board(session: str, board_id: Any = None) -> Dict[str, Any]:
    """The board a call is about: the id given, else the one on screen, else the chat's latest."""
    bid = str(board_id or "").strip()
    if bid:
        meta, _ = pizarra.get(bid)
        return meta
    st = surfaces.current(session, "pizarra")
    if st:
        try:
            return pizarra.get(st["id"])[0]
        except pizarra.BoardError:
            pass
    rows = pizarra.list_boards(session_id=session)
    if rows:
        return rows[0]
    raise pizarra.BoardError("no_board", "this chat has no board yet; call pizarra_create")


def _render_edit(res: Dict[str, Any], doc_counts: Dict[str, int]) -> str:
    lines = list(res["results"][:14])
    if len(res["results"]) > 14:
        lines.append(f"... and {len(res['results']) - 14} more ops")
    lines.append(f"Board {res['meta']['id']} rev {res['meta']['rev']}: {doc_counts['nodes']} items, {doc_counts['edges']} connectors.")
    return "\n".join(lines)


class PizarraCreateTool:
    async def execute(self, content: Any, ctx: dict) -> dict:
        a = _args(content)
        session = _session(ctx)
        owner = str((ctx or {}).get("owner") or "")
        meta = await asyncio.to_thread(pizarra.create, str(a.get("title") or ""), session_id=session,
                                       project_id=str((ctx or {}).get("project_id") or ""), owner=owner)
        surfaces.emit(session, "open", {"kind": "pizarra", "id": meta["id"], "title": meta["title"]})
        ops: List[Dict[str, Any]] = []
        if isinstance(a.get("ops"), list):
            ops = a["ops"]
        elif isinstance(a.get("items"), list):
            ops = [{"op": "outline", "items": a["items"], "layout": a.get("layout") or "tree",
                    "kind": a.get("kind") or "sticky", "frame_title": a.get("frame_title")}]
        text = f"Board {meta['title']!r} created, id {meta['id']}, open beside the chat."
        if ops:
            actor = surfaces.actor_for((ctx or {}).get("session_id"))
            try:
                res = await surfaces.pizarra_edit(session, meta["id"], ops, actor)
            except pizarra.BoardError as exc:
                return {"output": text + f" Filling it failed: {exc.message}", "exit_code": 0, "result": {"id": meta["id"]}}
            _, doc = await asyncio.to_thread(pizarra.get, meta["id"])
            text += "\n" + _render_edit(res, pizarra.counts(doc))
            if res.get("error"):
                text += "\nStopped: " + res["error"]["message"]
        return {"output": text, "exit_code": 0, "result": {"id": meta["id"], "title": meta["title"]}}


class PizarraReadTool:
    async def execute(self, content: Any, ctx: dict) -> dict:
        a = _args(content)
        session = _session(ctx)
        try:
            meta = resolve_board(session, a.get("id"))
            meta, doc = await asyncio.to_thread(pizarra.get, meta["id"])
        except pizarra.BoardError as exc:
            return _fail("pizarra_read", exc.code, exc.message)
        st = surfaces.current(session, "pizarra") or {}
        sel = st.get("selection") if st.get("id") == meta["id"] else []
        view = st.get("viewport") if st.get("id") == meta["id"] else None
        text = pizarra.outline(doc, title=meta["title"], board_id=meta["id"], rev=meta["rev"], region=a.get("region"),
                               selection=sel or [], viewport=view)
        return {"output": text, "exit_code": 0, "result": {"id": meta["id"], "rev": meta["rev"], **pizarra.counts(doc)}}


class PizarraEditTool:
    async def execute(self, content: Any, ctx: dict) -> dict:
        a = _args(content)
        session = _session(ctx)
        try:
            meta = resolve_board(session, a.get("id"))
            res = await surfaces.pizarra_edit(session, meta["id"], a.get("ops"), surfaces.actor_for((ctx or {}).get("session_id")))
            _, doc = await asyncio.to_thread(pizarra.get, meta["id"])
        except pizarra.BoardError as exc:
            return _fail("pizarra_edit", exc.code, exc.message)
        text = _render_edit(res, pizarra.counts(doc))
        if res.get("error"):
            return {"error": "pizarra_edit: " + res["error"]["message"] + "\nApplied before the error:\n" + text,
                    "error_class": "pizarra." + res["error"]["code"], "exit_code": 1}
        return {"output": text, "exit_code": 0, "result": {"id": meta["id"], "rev": res["meta"]["rev"]}}


class PizarraArrangeTool:
    async def execute(self, content: Any, ctx: dict) -> dict:
        a = _args(content)
        session = _session(ctx)
        layout = str(a.get("layout") or "grid")
        scope = a.get("scope", "all")
        try:
            meta = resolve_board(session, a.get("id"))
            _, doc = await asyncio.to_thread(pizarra.get, meta["id"])
            idx = pizarra.index_nodes(doc)
            op: Dict[str, Any] = {"op": "layout", "layout": layout}
            if a.get("cols"):
                op["cols"] = a["cols"]
            if a.get("root"):
                op["root"] = a["root"]
            if isinstance(scope, list):
                op["ids"] = [str(i) for i in scope]
            elif scope == "selected":
                st = surfaces.current(session, "pizarra") or {}
                sel = [s for s in (st.get("selection") or []) if s in idx] if st.get("id") == meta["id"] else []
                if not sel:
                    return _fail("pizarra_arrange", "bad_args", "the person has nothing selected on this board")
                op["ids"] = sel
            elif scope == "all" or scope is None:
                op["ids"] = [n["id"] for n in doc["nodes"] if pizarra.kind_of(n) not in ("frame", "stroke")]
            elif isinstance(scope, str) and scope in idx:
                op["frame"] = scope
            else:
                return _fail("pizarra_arrange", "bad_args", "`scope` is all, selected, a frame id or a list of ids")
            if not op.get("ids") and not op.get("frame"):
                return _fail("pizarra_arrange", "bad_args", "nothing to arrange")
            res = await surfaces.pizarra_edit(session, meta["id"], [op], surfaces.actor_for((ctx or {}).get("session_id")))
        except pizarra.BoardError as exc:
            return _fail("pizarra_arrange", exc.code, exc.message)
        if res.get("error"):
            return _fail("pizarra_arrange", res["error"]["code"], res["error"]["message"])
        return {"output": "; ".join(res["results"]) + f" (rev {res['meta']['rev']})", "exit_code": 0, "result": {"id": meta["id"]}}


HANDLERS = {"pizarra_create": PizarraCreateTool, "pizarra_read": PizarraReadTool, "pizarra_edit": PizarraEditTool,
            "pizarra_arrange": PizarraArrangeTool}
