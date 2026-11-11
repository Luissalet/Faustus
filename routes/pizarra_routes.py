"""routes/pizarra_routes.py -- HTTP surface of Pizarra, the whiteboard (src/pizarra.py).

GET    /api/surfaces/events?session=          server-sent events for the window: open, patch, cells, cursor, meta
GET    /api/pizarra?session=&project=         the boards of a chat or project
POST   /api/pizarra                           {title?, session?, project?, doc?}  create
GET    /api/pizarra/{id}                      {meta, doc}
POST   /api/pizarra/{id}/patch                {patch, client?}  the person's edit: merged per item, sent on to other windows
POST   /api/pizarra/{id}/ops                  {ops, session?}  what pizarra_edit does (file, windows, cursor); used by the MCP server
POST   /api/pizarra/{id}/state                {session, selection?, viewport?, open?}  what the person has on screen
POST   /api/pizarra/{id}/rename               {title}
DELETE /api/pizarra/{id}
GET    /api/pizarra/{id}/export.canvas        the JSON Canvas file
GET    /api/pizarra/{id}/export.svg?dark=     the board drawn as SVG (the window turns it into PNG)
POST   /api/pizarra/{id}/import               a .canvas file as the body: replaces the board (the old one stays in the history)
POST   /api/pizarra/{id}/asset?ext=           an image as the body -> {file}
GET    /api/pizarra/{id}/assets/{name}        the image
GET    /api/pizarra/{id}/history              snapshots
POST   /api/pizarra/{id}/restore              {rev}
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

from core.middleware import require_admin
from src import pizarra, surfaces

HEARTBEAT_S = 15.0


async def _json(request: Request) -> Dict[str, Any]:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    return body if isinstance(body, dict) else {}


def _fail(exc: pizarra.BoardError):
    status = {"not_found": 404, "too_big": 413, "no_board": 404}.get(exc.code, 400)
    return JSONResponse(status_code=status, content={"error": exc.message, "error_class": "pizarra." + exc.code})


def _sse(event: str, data: Dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


def setup_pizarra_routes() -> APIRouter:
    router = APIRouter(tags=["pizarra"])

    def guard(request: Request) -> None:
        require_admin(request)

    @router.get("/api/surfaces/events")
    async def events(request: Request, session: str = ""):
        guard(request)
        try:
            sub = surfaces.subscribe(session)
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"error": str(exc)})

        async def stream():
            try:
                yield "retry: 2000\n\n"
                yield _sse("hello", {"session": session})
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        item = await asyncio.wait_for(sub.queue.get(), timeout=HEARTBEAT_S)
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
                        continue
                    yield _sse(item["event"], item["data"])
            finally:
                surfaces.unsubscribe(sub)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @router.get("/api/pizarra")
    async def list_(request: Request, session: str = "", project: str = ""):
        guard(request)
        rows = await asyncio.to_thread(pizarra.list_boards, session_id=session or None, project_id=project or None)
        return {"boards": rows}

    @router.post("/api/pizarra")
    async def create(request: Request):
        guard(request)
        body = await _json(request)
        try:
            meta = await asyncio.to_thread(pizarra.create, str(body.get("title") or ""), session_id=str(body.get("session") or ""),
                                           project_id=str(body.get("project") or ""), doc=body.get("doc"))
        except pizarra.BoardError as exc:
            return _fail(exc)
        if meta.get("session_id"):
            surfaces.emit(meta["session_id"], "open", {"kind": "pizarra", "id": meta["id"], "title": meta["title"]})
        return meta

    @router.get("/api/pizarra/{board_id}")
    async def read(request: Request, board_id: str):
        guard(request)
        try:
            meta, doc = await asyncio.to_thread(pizarra.get, board_id)
        except pizarra.BoardError as exc:
            return _fail(exc)
        return {"meta": meta, "doc": doc}

    @router.post("/api/pizarra/{board_id}/patch")
    async def patch(request: Request, board_id: str):
        guard(request)
        body = await _json(request)
        try:
            meta, applied = await asyncio.to_thread(pizarra.patch, board_id, body.get("patch"))
        except pizarra.BoardError as exc:
            return _fail(exc)
        target = meta.get("session_id")
        if target and (applied["nodes"] or applied["edges"] or applied.get("order")):
            surfaces.emit(target, "patch", {"kind": "pizarra", "id": board_id, "rev": meta["rev"], "origin": str(body.get("client") or "person"),
                                            "actor": None, "patch": applied})
        return {"rev": meta["rev"]}

    @router.post("/api/pizarra/{board_id}/ops")
    async def ops(request: Request, board_id: str):
        guard(request)
        body = await _json(request)
        session = surfaces.top_session(str(body.get("session") or ""))
        try:
            res = await surfaces.pizarra_edit(session, board_id, body.get("ops"), surfaces.actor_for(str(body.get("session") or "")))
        except pizarra.BoardError as exc:
            return _fail(exc)
        return res

    @router.post("/api/pizarra/{board_id}/state")
    async def state(request: Request, board_id: str):
        guard(request)
        body = await _json(request)
        session = str(body.get("session") or "")
        if not session:
            return JSONResponse(status_code=400, content={"error": "`session` is required"})
        if body.get("open") is False:
            surfaces.forget(session, "pizarra", board_id)
            return {"open": False}
        sel = [str(s) for s in (body.get("selection") or []) if isinstance(s, str)][:200]
        vp = body.get("viewport") if isinstance(body.get("viewport"), dict) else None
        if vp:
            try:
                vp = {k: float(vp[k]) for k in ("x", "y", "zoom", "w", "h") if k in vp}
            except (TypeError, ValueError):
                vp = None
        surfaces.report(session, "pizarra", board_id, selection=sel, viewport=vp)
        return {"open": True}

    @router.post("/api/pizarra/{board_id}/rename")
    async def rename(request: Request, board_id: str):
        guard(request)
        body = await _json(request)
        try:
            meta = await asyncio.to_thread(pizarra.rename, board_id, str(body.get("title") or ""))
        except pizarra.BoardError as exc:
            return _fail(exc)
        if meta.get("session_id"):
            surfaces.emit(meta["session_id"], "meta", {"kind": "pizarra", "id": board_id, "title": meta["title"]})
        return meta

    @router.delete("/api/pizarra/{board_id}")
    async def delete(request: Request, board_id: str):
        guard(request)
        return {"deleted": await asyncio.to_thread(pizarra.delete, board_id)}

    @router.get("/api/pizarra/{board_id}/export.canvas")
    async def export_canvas(request: Request, board_id: str):
        guard(request)
        try:
            meta, doc = await asyncio.to_thread(pizarra.get, board_id)
        except pizarra.BoardError as exc:
            return _fail(exc)
        name = "".join(ch for ch in meta["title"] if ch.isalnum() or ch in " -_")[:60].strip() or "board"
        return Response(pizarra.dumps(doc), media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="{name}.canvas"'})

    @router.get("/api/pizarra/{board_id}/export.svg")
    async def export_svg(request: Request, board_id: str, dark: int = 0):
        guard(request)
        try:
            meta, doc = await asyncio.to_thread(pizarra.get, board_id)
        except pizarra.BoardError as exc:
            return _fail(exc)
        return Response(pizarra.to_svg(doc, dark=bool(dark)), media_type="image/svg+xml")

    @router.post("/api/pizarra/{board_id}/import")
    async def import_(request: Request, board_id: str):
        guard(request)
        raw = await request.body()
        try:
            meta, ch = await asyncio.to_thread(pizarra.replace_doc, board_id, raw)
            _, doc = await asyncio.to_thread(pizarra.get, board_id)
        except pizarra.BoardError as exc:
            return _fail(exc)
        if meta.get("session_id"):
            surfaces.emit(meta["session_id"], "patch", {"kind": "pizarra", "id": board_id, "rev": meta["rev"], "origin": "import",
                                                        "actor": None, "patch": ch.patch(doc)})
        return {"rev": meta["rev"], **pizarra.counts(doc)}

    @router.post("/api/pizarra/{board_id}/asset")
    async def asset(request: Request, board_id: str, ext: str = "png"):
        guard(request)
        data = await request.body()
        try:
            return {"file": await asyncio.to_thread(pizarra.add_asset, board_id, data, ext)}
        except pizarra.BoardError as exc:
            return _fail(exc)

    @router.get("/api/pizarra/{board_id}/assets/{name}")
    async def asset_get(request: Request, board_id: str, name: str):
        guard(request)
        try:
            path = pizarra.asset_path(board_id, name)
        except pizarra.BoardError as exc:
            return _fail(exc)
        if not path:
            return Response(status_code=404)
        return FileResponse(path, headers={"Cache-Control": "private, max-age=86400"})

    @router.get("/api/pizarra/{board_id}/history")
    async def history(request: Request, board_id: str):
        guard(request)
        try:
            return {"snapshots": await asyncio.to_thread(pizarra.history, board_id)}
        except pizarra.BoardError as exc:
            return _fail(exc)

    @router.post("/api/pizarra/{board_id}/restore")
    async def restore(request: Request, board_id: str):
        guard(request)
        body = await _json(request)
        try:
            meta, ch = await asyncio.to_thread(pizarra.restore, board_id, int(body.get("rev") or 0))
            _, doc = await asyncio.to_thread(pizarra.get, board_id)
        except pizarra.BoardError as exc:
            return _fail(exc)
        if meta.get("session_id"):
            surfaces.emit(meta["session_id"], "patch", {"kind": "pizarra", "id": board_id, "rev": meta["rev"], "origin": "restore",
                                                        "actor": None, "patch": ch.patch(doc)})
        return {"rev": meta["rev"]}

    return router
