"""Canvas-domain routes (WP20): server-side render/export of a ``canvas``
CreatorDocument, legacy import, and the bridge into WP11's inpaint recipe.

Same conventions as ``routes/creator_timeline_routes.py`` (WP13): thin HTTP
adapter over ``src/creator/canvas.py``, ``creator_enabled`` checked before
any store/asset access, owner always from the authenticated session,
"not yours" and "doesn't exist" both 404 (CONTRATO.md rule 3).
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request, Response

from src.auth_helpers import require_user


def _owner(request: Request) -> str:
    from core import middleware as mw
    from src.owner_identity import effective_storage_owner
    user = require_user(request)
    owner = effective_storage_owner(user, auth_is_disabled=mw.auth_disabled())
    return owner or ""


def _require_flag() -> None:
    from src.settings import get_setting
    if not bool(get_setting("creator_enabled", False)):
        raise HTTPException(status_code=404, detail="creator is not enabled")


async def _json_object(request: Request) -> Dict[str, Any]:
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "Request body must be JSON")
    if not isinstance(payload, dict):
        raise HTTPException(400, "Request body must be a JSON object")
    return payload


def _doc_error(exc: Exception) -> None:
    from src.creator.errors import (
        DocumentNotFound, InvalidDocument, InvalidOperation, RevisionConflict,
    )
    from src.creator.canvas import CanvasError
    if isinstance(exc, DocumentNotFound):
        raise HTTPException(404, "document not found")
    if isinstance(exc, RevisionConflict):
        raise HTTPException(409, {"reason": "revision_conflict", "current_revision": exc.current_revision})
    if isinstance(exc, CanvasError):
        raise HTTPException(400, str(exc))
    if isinstance(exc, (InvalidDocument, InvalidOperation)):
        raise HTTPException(400, str(exc))
    raise


def _get_canvas_doc(owner: str, doc_id: str):
    from src.creator.store import get_store
    doc = get_store().get(owner, doc_id)
    if doc is None:
        raise HTTPException(404, "document not found")
    if doc.kind != "canvas":
        raise HTTPException(400, f"document {doc_id!r} is not a canvas document (kind={doc.kind!r})")
    return doc


def setup_creator_canvas_routes() -> APIRouter:
    router = APIRouter(prefix="/api/creator", tags=["creator-canvas"])

    @router.get("/canvas/{doc_id}/render")
    def render_canvas(request: Request, doc_id: str, revision: int = 0):
        """PNG bytes of the composed document, at ``revision`` (its current
        revision if omitted/0). Deterministic: the same document/revision
        always returns the same bytes, so a client can cache on
        ``(doc_id, revision)`` without an ETag round trip."""
        owner = _owner(request)
        _require_flag()
        doc = _get_canvas_doc(owner, doc_id)
        content = doc.content
        rev_used = doc.revision
        if revision and revision != doc.revision:
            from src.creator.store import get_store
            snap = get_store().get_revision_snapshot(owner, doc_id, revision)
            if snap is None:
                raise HTTPException(404, "revision not found")
            content = snap["content"]
            rev_used = snap["revision"]

        from src.creator import canvas as canvas_mod
        try:
            data = canvas_mod.render(content, owner)
        except Exception as exc:
            _doc_error(exc)
            raise
        return Response(content=data, media_type="image/png",
                        headers={"ETag": str(rev_used), "Cache-Control": "private, max-age=3600, immutable"})

    @router.post("/canvas/{doc_id}/export")
    async def export_canvas(request: Request, doc_id: str):
        owner = _owner(request)
        _require_flag()
        doc = _get_canvas_doc(owner, doc_id)
        payload = await _json_object(request) if request.headers.get("content-length") not in (None, "0") else {}
        fmt = str(payload.get("format") or "png")

        from src.creator import canvas as canvas_mod
        try:
            result = canvas_mod.export(doc, owner, fmt=fmt)
        except Exception as exc:
            _doc_error(exc)
            raise
        return result

    @router.post("/canvas/import-legacy")
    async def import_legacy_canvas(request: Request):
        owner = _owner(request)
        _require_flag()
        payload = await _json_object(request)
        edit_project_id = str(payload.get("edit_project_id") or "")
        project_id = str(payload.get("project_id") or "")
        if not edit_project_id:
            raise HTTPException(400, "edit_project_id is required")
        if not project_id:
            raise HTTPException(400, "project_id is required")

        from src.creator import canvas as canvas_mod
        from src.creator.store import get_store
        try:
            content = canvas_mod.import_legacy(edit_project_id, owner, project_id)
        except Exception as exc:
            _doc_error(exc)
            raise
        doc = get_store().create(owner, project_id, "canvas", content)
        return doc.to_public_dict()

    @router.post("/canvas/{doc_id}/inpaint-request")
    async def inpaint_request_canvas(request: Request, doc_id: str):
        owner = _owner(request)
        _require_flag()
        doc = _get_canvas_doc(owner, doc_id)
        payload = await _json_object(request)
        region = payload.get("region")
        prompt = str(payload.get("prompt") or "")
        if not isinstance(region, dict):
            raise HTTPException(400, "region is required and must be an object")

        from src.creator import canvas as canvas_mod
        try:
            result = canvas_mod.inpaint_request(doc, region, prompt, owner)
        except Exception as exc:
            _doc_error(exc)
            raise
        return result

    return router
