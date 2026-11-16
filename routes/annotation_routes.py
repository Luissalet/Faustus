"""routes/annotation_routes.py -- HTTP surface of annotations and guided review (src/annotations.py).

GET    /api/annotations?session=                    {target, annotations, pending, review}
POST   /api/annotations                             {session, target, type, quote?, text?, anchor?}  add one
PATCH  /api/annotations/{id}                        {session, text?, type?, pending?}
DELETE /api/annotations/{id}?session=
POST   /api/annotations/clear                       {session, target?, sent_only?}
POST   /api/annotations/compose                     {session, ids?, target?, mark_sent?}  -> {text, count, ids}
POST   /api/annotations/open                        {session, target}  the Annotate tab shows it (web: proxied)
POST   /api/annotations/web                         {url} | {path} | {html}  -> {upstream, url, origin}: a local page,
                                                    an HTML file or HTML text, framable with the annotate script
POST   /api/annotations/review                      {session, workspace?, base?, diff?}  open a review
GET    /api/annotations/review?session=             the open review: files, hunks, lines, guide
POST   /api/annotations/review/guide                {session, model?}  chapters from the chat's model
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from core.middleware import require_admin
from src import annotations, surfaces


async def _json(request: Request) -> Dict[str, Any]:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    return body if isinstance(body, dict) else {}


def _fail(exc: annotations.AnnotationError) -> JSONResponse:
    status = {"not_found": 404, "no_review": 404, "too_many": 413, "no_model": 503, "timeout": 504,
              "unparsed": 502}.get(exc.code, 400)
    return JSONResponse(status_code=status, content={"error": exc.message, "error_class": "annotations." + exc.code})


def open_target(session: str, target: Any) -> Dict[str, Any]:
    """Save the chat's target and tell its Studio window; a web target also gets its proxy."""
    tgt = annotations.set_target(session, target)
    out: Dict[str, Any] = {"target": tgt}
    if tgt["kind"] == "web":
        from src import annotation_proxy
        out["web"] = annotation_proxy.ensure(tgt["ref"])
    elif tgt["kind"] == "file" and tgt["ref"].lower().endswith((".html", ".htm")):
        from src import annotation_proxy
        out["web"] = annotation_proxy.serve_file(tgt["ref"])
    surfaces.emit(surfaces.top_session(session) or session, "open", {"kind": "anotar", "target": tgt, **({"web": out["web"]} if "web" in out else {})})
    return out


def setup_annotation_routes() -> APIRouter:
    router = APIRouter(tags=["annotations"])

    def guard(request: Request) -> None:
        require_admin(request)

    @router.get("/api/annotations")
    async def get_state(request: Request, session: str = ""):
        guard(request)
        try:
            return await asyncio.to_thread(annotations.state, session)
        except annotations.AnnotationError as exc:
            return _fail(exc)

    @router.post("/api/annotations")
    async def add(request: Request):
        guard(request)
        b = await _json(request)
        try:
            note = await asyncio.to_thread(annotations.add, str(b.get("session") or ""), b.get("target"),
                                           str(b.get("type") or "comment"), str(b.get("quote") or ""),
                                           str(b.get("text") or ""), b.get("anchor"), str(b.get("author") or "person"))
        except annotations.AnnotationError as exc:
            return _fail(exc)
        return note

    @router.patch("/api/annotations/{note_id}")
    async def patch(request: Request, note_id: str):
        guard(request)
        b = await _json(request)
        try:
            return await asyncio.to_thread(annotations.update, str(b.get("session") or ""), note_id,
                                           text=b.get("text"), type=b.get("type"), pending=bool(b.get("pending")))
        except annotations.AnnotationError as exc:
            return _fail(exc)

    @router.delete("/api/annotations/{note_id}")
    async def remove(request: Request, note_id: str, session: str = ""):
        guard(request)
        try:
            n = await asyncio.to_thread(annotations.delete, session, note_id)
        except annotations.AnnotationError as exc:
            return _fail(exc)
        if not n:
            return JSONResponse(status_code=404, content={"error": "no such annotation", "error_class": "annotations.not_found"})
        return {"deleted": n}

    @router.post("/api/annotations/clear")
    async def clear(request: Request):
        guard(request)
        b = await _json(request)
        try:
            n = await asyncio.to_thread(annotations.delete, str(b.get("session") or ""), None, b.get("target"),
                                        bool(b.get("sent_only")))
        except annotations.AnnotationError as exc:
            return _fail(exc)
        return {"deleted": n}

    @router.post("/api/annotations/compose")
    async def compose(request: Request):
        guard(request)
        b = await _json(request)
        ids = b.get("ids") if isinstance(b.get("ids"), list) else None
        try:
            return await asyncio.to_thread(annotations.compose, str(b.get("session") or ""), ids, b.get("target"),
                                           b.get("mark_sent", True) is not False, bool(b.get("include_sent")))
        except annotations.AnnotationError as exc:
            return _fail(exc)

    @router.post("/api/annotations/open")
    async def open_(request: Request):
        guard(request)
        b = await _json(request)
        try:
            return await asyncio.to_thread(open_target, str(b.get("session") or ""), b.get("target"))
        except annotations.AnnotationError as exc:
            return _fail(exc)

    @router.post("/api/annotations/web")
    async def web(request: Request):
        guard(request)
        b = await _json(request)
        from src import annotation_proxy
        try:
            if isinstance(b.get("html"), str):
                return await asyncio.to_thread(annotation_proxy.serve_html, b["html"], str(b.get("name") or "index.html"))
            if b.get("path"):
                return await asyncio.to_thread(annotation_proxy.serve_file, str(b["path"]))
            return await asyncio.to_thread(annotation_proxy.ensure, str(b.get("url") or ""))
        except annotations.AnnotationError as exc:
            return _fail(exc)

    @router.post("/api/annotations/review")
    async def review(request: Request):
        guard(request)
        b = await _json(request)
        session = str(b.get("session") or "")
        try:
            res = await asyncio.to_thread(annotations.open_review, session, str(b.get("workspace") or ""),
                                          str(b.get("base") or "HEAD"), str(b.get("diff") or ""))
        except annotations.AnnotationError as exc:
            return _fail(exc)
        if session:
            surfaces.emit(surfaces.top_session(session) or session, "open",
                          {"kind": "anotar", "target": {"kind": "diff", "ref": f"{res['workspace']}@{res['base']}",
                                                        "title": f"Cambios frente a {res['base']}"}})
        return res

    @router.get("/api/annotations/review")
    async def review_get(request: Request, session: str = ""):
        guard(request)
        try:
            rv = await asyncio.to_thread(annotations.current_review, session)
        except annotations.AnnotationError as exc:
            return _fail(exc)
        if not rv:
            return JSONResponse(status_code=404, content={"error": "no review is open in this chat", "error_class": "annotations.no_review"})
        return rv

    @router.post("/api/annotations/review/guide")
    async def review_guide(request: Request):
        guard(request)
        b = await _json(request)
        from src.auth_helpers import effective_user
        try:
            return await annotations.guide(str(b.get("session") or ""), owner=effective_user(request) or None,
                                           model=str(b.get("model") or ""))
        except annotations.AnnotationError as exc:
            return _fail(exc)

    return router
