"""routes/text_fold_routes.py - fold a long text into five levels of detail.

``POST /api/fold`` builds (or reuses) the folded document of a text or of a stored
artifact and, when ``level`` is given, returns that level. ``GET /api/fold/{id}`` reads one
level of a stored document (all of it, one section, or the original of one paragraph) and
``GET /api/fold/{id}/document`` returns the open document (``faustus.fold/1``). Everything
is scoped to the caller; levels 3-5 are generated on demand and cached with the document.
Errors are flat: ``{"error", "error_class"}``.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from src import text_fold


def _owner(request: Request) -> str:
    from core import middleware
    from src.auth_helpers import effective_user
    from src.owner_identity import effective_storage_owner
    owner = effective_storage_owner(effective_user(request), auth_is_disabled=middleware.auth_disabled())
    if not owner:
        raise HTTPException(401, "An authenticated owner is required")
    return owner


def _error(exc: Exception) -> JSONResponse:
    if isinstance(exc, text_fold.FoldNotFound):
        return JSONResponse(status_code=404, content={"error": str(exc), "error_class": "fold.not_found"})
    if isinstance(exc, text_fold.FoldUnavailable):
        return JSONResponse(status_code=503, content={"error": str(exc), "error_class": "fold.model_unavailable"})
    return JSONResponse(status_code=400, content={"error": str(exc), "error_class": "fold.invalid"})


class FoldRequest(BaseModel):
    text: Optional[str] = None
    artifact_id: Optional[str] = None
    text_pointer: Optional[str] = None
    level: Optional[int] = None
    section: Optional[str] = None
    paragraph: Optional[str] = None


async def fold_source(owner: str, body: FoldRequest) -> Dict[str, Any]:
    """Shared by the route and the MCP server: the document summary plus, if asked, a level."""
    if bool(body.text) == bool(body.artifact_id):
        raise text_fold.FoldError("give exactly one of text or artifact_id")
    text = body.text if body.text else text_fold.text_of_artifact(owner, body.artifact_id, body.text_pointer)
    doc = text_fold.open_document(owner, text)
    out: Dict[str, Any] = {"document": text_fold.summary(doc), "view": None}
    if body.level is not None or body.section or body.paragraph:
        out["view"] = await text_fold.read(owner, doc["id"], body.level or 1, body.section, body.paragraph)
        out["document"] = text_fold.summary(text_fold.load(owner, doc["id"]))
    return out


def setup_text_fold_routes() -> APIRouter:
    router = APIRouter(tags=["text-fold"])

    @router.post("/api/fold")
    async def fold_route(request: Request, body: FoldRequest):
        owner = _owner(request)
        try:
            return await fold_source(owner, body)
        except (text_fold.FoldError, text_fold.FoldUnavailable) as exc:
            return _error(exc)

    @router.get("/api/fold/{fold_id}")
    async def fold_read_route(request: Request, fold_id: str, level: int = 1, section: str = "", paragraph: str = ""):
        owner = _owner(request)
        try:
            return await text_fold.read(owner, fold_id, level, section or None, paragraph or None)
        except (text_fold.FoldError, text_fold.FoldUnavailable) as exc:
            return _error(exc)

    @router.get("/api/fold/{fold_id}/document")
    async def fold_document_route(request: Request, fold_id: str):
        owner = _owner(request)
        try:
            return text_fold.load(owner, fold_id)
        except text_fold.FoldError as exc:
            return _error(exc)

    return router
