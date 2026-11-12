"""routes/sheet_routes.py -- HTTP surface of Hoja, the spreadsheet (src/hoja.py).

GET    /api/hoja?session=&project=             the sheets of a chat or project
POST   /api/hoja                               {title?, session?, project?, csv?, tab?}  create
GET    /api/hoja/{id}                          {meta, tabs: [tab meta: used range, widths, freeze, filter, conditional formats, charts]}
GET    /api/hoja/{id}/window?tab=&r0=&c0=&r1=&c1=   the cells of a viewport (the grid is virtual: it only asks for what is visible)
GET    /api/hoja/{id}/tab/{tab}                tab meta
GET    /api/hoja/{id}/read?session=&tab=&range=     what sheet_read shows -> {text}
POST   /api/hoja/{id}/query                    {tab?, spec}  what sheet_query shows -> {text, data}
POST   /api/hoja/{id}/edit                     {ops, client?, session?, view?}  the person's edit: applied, sent on to other windows
POST   /api/hoja/{id}/ops                      {ops, session?}  what sheet_write does (file, windows, cursor); used by the MCP server
POST   /api/hoja/{id}/undo | /redo             step the history
POST   /api/hoja/{id}/state                    {session, tab?, sel?, active?, open?}  what the person has on screen
POST   /api/hoja/{id}/rename                   {title}
DELETE /api/hoja/{id}
GET    /api/hoja/{id}/export.xlsx              the .xlsx on disk (the durable artifact)
GET    /api/hoja/{id}/export.csv?tab=
POST   /api/hoja/{id}/import?format=xlsx|csv   the file as the body: xlsx replaces the workbook (undoable), csv is added at A1 of the tab
POST   /api/hoja/open-file                     {workspace, path, session?, project?}  a copy of a workspace .xlsx / .csv as a new sheet
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from core.middleware import require_admin
from src import hoja, surfaces


async def _json(request: Request) -> Dict[str, Any]:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    return body if isinstance(body, dict) else {}


def _fail(exc: hoja.SheetError):
    status = {"not_found": 404, "no_sheet": 404, "too_big": 413}.get(exc.code, 400)
    return JSONResponse(status_code=status, content={"error": exc.message, "error_class": "hoja." + exc.code})


def _tabs(sheet_id: str) -> Dict[str, Any]:
    with hoja._lock:  # noqa: SLF001
        meta, wb = hoja.get(sheet_id)
        return {"meta": meta, "tabs": [hoja.tab_meta(wb, t) for t in wb.tabs]}


def _window(sheet_id: str, tab: str, r0: int, c0: int, r1: int, c1: int) -> Dict[str, Any]:
    with hoja._lock:  # noqa: SLF001
        meta, wb = hoja.get(sheet_id)
        t = wb.tab(tab) if tab else wb.tabs[0]
        if t is None:
            raise hoja.SheetError("not_found", f"no tab {tab!r}")
        return {"rev": meta["rev"], "tab": t.id, **hoja.window(wb, t, max(1, r0), max(1, c0), max(1, r1), max(1, c1))}


def _tab_meta(sheet_id: str, tab: str) -> Dict[str, Any]:
    with hoja._lock:  # noqa: SLF001
        meta, wb = hoja.get(sheet_id)
        t = wb.tab(tab)
        if t is None:
            raise hoja.SheetError("not_found", f"no tab {tab!r}")
        return {"rev": meta["rev"], **hoja.tab_meta(wb, t)}


def _read(sheet_id: str, session: str, tab: str, rng: str) -> str:
    st = surfaces.current(session, "hoja") if session else None
    with hoja._lock:  # noqa: SLF001
        meta, wb = hoja.get(sheet_id)
        state = {"tab": st.get("tab"), "sel": st.get("sel")} if st and st.get("id") == sheet_id else None
        return hoja.read_text(wb, meta, tab=tab or None, rng=rng or None, state=state)


def _query(sheet_id: str, tab: Any, spec: Dict[str, Any]):
    with hoja._lock:  # noqa: SLF001
        _, wb = hoja.get(sheet_id)
        return hoja.query(wb, tab, spec)


def setup_sheet_routes() -> APIRouter:
    router = APIRouter(tags=["hoja"])

    def guard(request: Request) -> None:
        require_admin(request)
        hoja.F.set_locale(request.headers.get("x-hoja-locale") or request.query_params.get("locale"))   # how cells are shown and typed numbers read

    @router.get("/api/hoja")
    async def list_(request: Request, session: str = "", project: str = ""):
        guard(request)
        rows = await asyncio.to_thread(hoja.list_sheets, session_id=session or None, project_id=project or None)
        return {"sheets": rows}

    @router.post("/api/hoja")
    async def create(request: Request):
        guard(request)
        body = await _json(request)
        try:
            meta = await asyncio.to_thread(hoja.create, str(body.get("title") or ""), session_id=str(body.get("session") or ""),
                                           project_id=str(body.get("project") or ""),
                                           csv_text=body.get("csv") if isinstance(body.get("csv"), str) else None,
                                           tab_name=str(body.get("tab") or "Sheet1"))
        except hoja.SheetError as exc:
            return _fail(exc)
        if meta.get("session_id"):
            surfaces.emit(meta["session_id"], "open", {"kind": "hoja", "id": meta["id"], "title": meta["title"]})
        return meta

    @router.post("/api/hoja/open-file")
    async def open_file(request: Request):
        guard(request)
        body = await _json(request)
        from src.tool_execution import _resolve_tool_path_in_roots, vet_workspace
        root = vet_workspace(str(body.get("workspace") or ""))
        if not root:
            return JSONResponse(status_code=400, content={"error": "workspace is not a valid folder", "error_class": "hoja.bad_args"})
        try:
            target = _resolve_tool_path_in_roots([root], str(body.get("path") or ""), root)
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"error": str(exc), "error_class": "hoja.bad_args"})
        import os
        if not os.path.isfile(target) or os.path.getsize(target) > 40_000_000:
            return JSONResponse(status_code=404, content={"error": "file not found or too large", "error_class": "hoja.not_found"})

        def make():
            with open(target, "rb") as fh:
                raw = fh.read()
            name = os.path.splitext(os.path.basename(target))[0]
            if target.lower().endswith(".csv"):
                return hoja.create(name, session_id=str(body.get("session") or ""), project_id=str(body.get("project") or ""),
                                   csv_text=raw.decode("utf-8-sig", errors="replace"))
            return hoja.create(name, session_id=str(body.get("session") or ""), project_id=str(body.get("project") or ""), xlsx_bytes=raw)

        try:
            meta = await asyncio.to_thread(make)
        except hoja.SheetError as exc:
            return _fail(exc)
        except Exception:  # noqa: BLE001
            return JSONResponse(status_code=400, content={"error": "that file could not be read", "error_class": "hoja.bad_file"})
        if meta.get("session_id"):
            surfaces.emit(meta["session_id"], "open", {"kind": "hoja", "id": meta["id"], "title": meta["title"]})
        return meta

    @router.get("/api/hoja/{sheet_id}")
    async def read(request: Request, sheet_id: str):
        guard(request)
        try:
            return await asyncio.to_thread(_tabs, sheet_id)
        except hoja.SheetError as exc:
            return _fail(exc)

    @router.get("/api/hoja/{sheet_id}/window")
    async def window(request: Request, sheet_id: str, tab: str = "", r0: int = 1, c0: int = 1, r1: int = 60, c1: int = 20):
        guard(request)
        try:
            return await asyncio.to_thread(_window, sheet_id, tab, r0, c0, r1, c1)
        except hoja.SheetError as exc:
            return _fail(exc)

    @router.get("/api/hoja/{sheet_id}/tab/{tab}")
    async def tab_meta(request: Request, sheet_id: str, tab: str):
        guard(request)
        try:
            return await asyncio.to_thread(_tab_meta, sheet_id, tab)
        except hoja.SheetError as exc:
            return _fail(exc)

    @router.get("/api/hoja/{sheet_id}/read")
    async def read_text(request: Request, sheet_id: str, session: str = "", tab: str = "", range: str = ""):  # noqa: A002
        guard(request)
        try:
            return {"text": await asyncio.to_thread(_read, sheet_id, session, tab, range)}
        except hoja.SheetError as exc:
            return _fail(exc)

    @router.post("/api/hoja/{sheet_id}/query")
    async def query(request: Request, sheet_id: str):
        guard(request)
        body = await _json(request)
        try:
            text, data = await asyncio.to_thread(_query, sheet_id, body.get("tab"), body.get("spec") if isinstance(body.get("spec"), dict) else {})
        except hoja.SheetError as exc:
            return _fail(exc)
        return {"text": text, "data": data}

    @router.post("/api/hoja/{sheet_id}/edit")
    async def edit(request: Request, sheet_id: str):
        guard(request)
        body = await _json(request)
        view = body.get("view") if isinstance(body.get("view"), dict) else None
        try:
            meta, results, ch, err = await asyncio.to_thread(hoja.edit, sheet_id, body.get("ops"), view=view)
        except hoja.SheetError as exc:
            return _fail(exc)
        target = meta.get("session_id")
        if target and not ch.empty():
            surfaces.emit(target, "cells", {"kind": "hoja", "id": sheet_id, "rev": meta["rev"], "origin": str(body.get("client") or "person"),
                                            "actor": None, **ch.payload()})
        out: Dict[str, Any] = {"rev": meta["rev"], "results": results, **ch.payload()}
        if err is not None:
            out["error"] = {"code": err.code, "message": err.message}
        return out

    @router.post("/api/hoja/{sheet_id}/ops")
    async def ops(request: Request, sheet_id: str):
        guard(request)
        body = await _json(request)
        session = surfaces.top_session(str(body.get("session") or ""))
        try:
            res = await surfaces.sheet_write(session, sheet_id, body.get("ops"), surfaces.actor_for(str(body.get("session") or "")))
        except hoja.SheetError as exc:
            return _fail(exc)
        return {"results": res["results"], "rev": res["meta"]["rev"], **({"error": res["error"]} if res.get("error") else {})}

    async def _step(request: Request, sheet_id: str, fn):
        guard(request)
        body = await _json(request)
        try:
            meta, ch, ok = await asyncio.to_thread(fn, sheet_id)
        except hoja.SheetError as exc:
            return _fail(exc)
        target = meta.get("session_id")
        if ok and target:
            surfaces.emit(target, "cells", {"kind": "hoja", "id": sheet_id, "rev": meta["rev"], "origin": str(body.get("client") or "person"),
                                            "actor": None, **ch.payload()})
        return {"ok": ok, "rev": meta["rev"], **ch.payload()}

    @router.post("/api/hoja/{sheet_id}/undo")
    async def undo(request: Request, sheet_id: str):
        return await _step(request, sheet_id, hoja.undo)

    @router.post("/api/hoja/{sheet_id}/redo")
    async def redo(request: Request, sheet_id: str):
        return await _step(request, sheet_id, hoja.redo)

    @router.post("/api/hoja/{sheet_id}/state")
    async def state(request: Request, sheet_id: str):
        guard(request)
        body = await _json(request)
        session = str(body.get("session") or "")
        if not session:
            return JSONResponse(status_code=400, content={"error": "`session` is required"})
        if body.get("open") is False:
            surfaces.forget(session, "hoja", sheet_id)
            return {"open": False}
        sel = body.get("sel")
        if isinstance(sel, dict):
            try:
                sel = {k: int(sel[k]) for k in ("r0", "c0", "r1", "c1")}
            except (KeyError, TypeError, ValueError):
                sel = None
        else:
            sel = None
        surfaces.report(session, "hoja", sheet_id, tab=str(body.get("tab") or "") or None, sel=sel)
        return {"open": True}

    @router.post("/api/hoja/{sheet_id}/rename")
    async def rename(request: Request, sheet_id: str):
        guard(request)
        body = await _json(request)
        try:
            meta = await asyncio.to_thread(hoja.rename, sheet_id, str(body.get("title") or ""))
        except hoja.SheetError as exc:
            return _fail(exc)
        if meta.get("session_id"):
            surfaces.emit(meta["session_id"], "meta", {"kind": "hoja", "id": sheet_id, "title": meta["title"]})
        return meta

    @router.delete("/api/hoja/{sheet_id}")
    async def delete(request: Request, sheet_id: str):
        guard(request)
        return {"deleted": await asyncio.to_thread(hoja.delete, sheet_id)}

    def _name(meta: Dict[str, Any]) -> str:
        return "".join(ch for ch in meta["title"] if ch.isalnum() or ch in " -_")[:60].strip() or "sheet"

    @router.get("/api/hoja/{sheet_id}/export.xlsx")
    async def export_xlsx(request: Request, sheet_id: str):
        guard(request)
        try:
            meta, _ = await asyncio.to_thread(hoja.get, sheet_id)
        except hoja.SheetError as exc:
            return _fail(exc)
        path = await asyncio.to_thread(hoja.xlsx_path, sheet_id)
        if not path:
            return JSONResponse(status_code=500, content={"error": "could not write the .xlsx", "error_class": "hoja.xlsx"})
        return FileResponse(path, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            headers={"Content-Disposition": f'attachment; filename="{_name(meta)}.xlsx"'})

    @router.get("/api/hoja/{sheet_id}/export.csv")
    async def export_csv(request: Request, sheet_id: str, tab: str = ""):
        guard(request)
        try:
            meta, _ = await asyncio.to_thread(hoja.get, sheet_id)
            text = await asyncio.to_thread(hoja.export_csv, sheet_id, tab or None)
        except hoja.SheetError as exc:
            return _fail(exc)
        return Response(text.encode("utf-8-sig"), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{_name(meta)}.csv"'})

    @router.post("/api/hoja/{sheet_id}/import")
    async def import_(request: Request, sheet_id: str, format: str = "xlsx", tab: str = ""):  # noqa: A002
        guard(request)
        raw = await request.body()
        try:
            if format == "csv":
                meta, results, ch, err = await asyncio.to_thread(
                    hoja.edit, sheet_id, [{"op": "csv", "text": raw.decode("utf-8-sig", errors="replace"), "at": "A1", **({"tab": tab} if tab else {})}])
                if err is not None:
                    return _fail(err)
            else:
                meta, ch = await asyncio.to_thread(hoja.replace_from_xlsx, sheet_id, raw)
        except hoja.SheetError as exc:
            return _fail(exc)
        except Exception:  # noqa: BLE001
            return JSONResponse(status_code=400, content={"error": "that file could not be read", "error_class": "hoja.bad_file"})
        if meta.get("session_id"):
            surfaces.emit(meta["session_id"], "cells", {"kind": "hoja", "id": sheet_id, "rev": meta["rev"], "origin": "import",
                                                        "actor": None, **ch.payload()})
        return {"rev": meta["rev"]}

    return router
