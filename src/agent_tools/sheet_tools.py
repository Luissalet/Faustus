"""agent_tools/sheet_tools.py -- the four Hoja (spreadsheet) tools (src/hoja.py, src/surfaces.py).

    sheet_create   a new workbook in this chat (empty, from rows, CSV or ops), opened beside the chat
    sheet_read     values / formulas / formats of a range, the person's selection, used range and a header summary per tab
    sheet_write    a batch of ops (set, fill, format, tabs, insert/delete, sort, filter, conditional formats, charts, csv)
    sheet_query    aggregates by column without dumping cells (sum, avg, min, max, count, distinct, find, head)

The workbook is an .xlsx on the server (plus a JSON working model). The open window shows the agent's changes as
they land, with the agent's named cursor. Nothing here needs a window: without one the file is simply updated.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List

from src import hoja, surfaces


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
    return {"error": f"{tool}: {message}", "error_class": "hoja." + code, "exit_code": 1}


def _session(ctx: dict) -> str:
    return surfaces.top_session((ctx or {}).get("session_id"))


def resolve_sheet(session: str, sheet_id: Any = None) -> Dict[str, Any]:
    """The workbook a call is about: the id given, else the one on screen, else the chat's latest."""
    sid = str(sheet_id or "").strip()
    if sid:
        return hoja.get(sid)[0]
    st = surfaces.current(session, "hoja")
    if st:
        try:
            return hoja.get(st["id"])[0]
        except hoja.SheetError:
            pass
    rows = hoja.list_sheets(session_id=session)
    if rows:
        return rows[0]
    raise hoja.SheetError("no_sheet", "this chat has no spreadsheet yet; call sheet_create")


def _view(session: str, sheet_id: str) -> Dict[str, Any]:
    st = surfaces.current(session, "hoja") or {}
    return {"tab": st.get("tab"), "sel": st.get("sel")} if st.get("id") == sheet_id else {}


def _render_edit(res: Dict[str, Any]) -> str:
    lines = list(res["results"][:14])
    if len(res["results"]) > 14:
        lines.append(f"... and {len(res['results']) - 14} more ops")
    lines.append(f"Sheet {res['meta']['id']} rev {res['meta']['rev']}.")
    return "\n".join(lines)


class SheetCreateTool:
    async def execute(self, content: Any, ctx: dict) -> dict:
        a = _args(content)
        session = _session(ctx)
        owner = str((ctx or {}).get("owner") or "")
        csv_text = a.get("csv") if isinstance(a.get("csv"), str) else None
        try:
            meta = await asyncio.to_thread(hoja.create, str(a.get("title") or ""), session_id=session,
                                           project_id=str((ctx or {}).get("project_id") or ""), owner=owner,
                                           csv_text=csv_text, tab_name=str(a.get("tab") or "Sheet1"))
        except hoja.SheetError as exc:
            return _fail("sheet_create", exc.code, exc.message)
        surfaces.emit(session, "open", {"kind": "hoja", "id": meta["id"], "title": meta["title"]})
        ops: List[Dict[str, Any]] = []
        if isinstance(a.get("ops"), list):
            ops = a["ops"]
        elif isinstance(a.get("rows"), list) and a["rows"]:
            ops = [{"op": "set", "at": "A1", "values": a["rows"]}]
        text = f"Sheet {meta['title']!r} created, id {meta['id']}, open beside the chat."
        if ops:
            try:
                res = await surfaces.sheet_write(session, meta["id"], ops, surfaces.actor_for((ctx or {}).get("session_id")))
            except hoja.SheetError as exc:
                return {"output": text + f" Filling it failed: {exc.message}", "exit_code": 0, "result": {"id": meta["id"]}}
            text += "\n" + _render_edit(res)
            if res.get("error"):
                text += "\nStopped: " + res["error"]["message"]
        return {"output": text, "exit_code": 0, "result": {"id": meta["id"], "title": meta["title"]}}


class SheetReadTool:
    async def execute(self, content: Any, ctx: dict) -> dict:
        a = _args(content)
        session = _session(ctx)
        try:
            meta = resolve_sheet(session, a.get("id"))
            meta, wb = await asyncio.to_thread(hoja.get, meta["id"])
            st = surfaces.current(session, "hoja") or {}
            state = {"tab": st.get("tab"), "sel": st.get("sel")} if st.get("id") == meta["id"] else None
            text = await asyncio.to_thread(hoja.read_text, wb, meta, tab=a.get("tab"), rng=a.get("range"), state=state)
        except hoja.SheetError as exc:
            return _fail("sheet_read", exc.code, exc.message)
        return {"output": text, "exit_code": 0, "result": {"id": meta["id"], "rev": meta["rev"]}}


class SheetWriteTool:
    async def execute(self, content: Any, ctx: dict) -> dict:
        a = _args(content)
        session = _session(ctx)
        try:
            meta = resolve_sheet(session, a.get("id"))
            res = await surfaces.sheet_write(session, meta["id"], a.get("ops"), surfaces.actor_for((ctx or {}).get("session_id")))
        except hoja.SheetError as exc:
            return _fail("sheet_write", exc.code, exc.message)
        text = _render_edit(res)
        if res.get("error"):
            return {"error": "sheet_write: " + res["error"]["message"] + "\nApplied before the error:\n" + text,
                    "error_class": "hoja." + res["error"]["code"], "exit_code": 1}
        return {"output": text, "exit_code": 0, "result": {"id": meta["id"], "rev": res["meta"]["rev"]}}


class SheetQueryTool:
    async def execute(self, content: Any, ctx: dict) -> dict:
        a = _args(content)
        session = _session(ctx)
        try:
            meta = resolve_sheet(session, a.get("id"))
            _, wb = await asyncio.to_thread(hoja.get, meta["id"])
            spec = {k: v for k, v in a.items() if k not in ("id", "tab")}
            text, data = await asyncio.to_thread(hoja.query, wb, a.get("tab"), spec)
        except hoja.SheetError as exc:
            return _fail("sheet_query", exc.code, exc.message)
        return {"output": text, "exit_code": 0, "result": {"id": meta["id"], **(data if isinstance(data, dict) else {})}}


HANDLERS = {"sheet_create": SheetCreateTool, "sheet_read": SheetReadTool, "sheet_write": SheetWriteTool,
            "sheet_query": SheetQueryTool}
