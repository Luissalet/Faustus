"""Timeline-domain routes (WP13): read-only projections over a ``timeline``
CreatorDocument (compact view for the Studio, a validation report, a
minimal EDL export) plus the one route no earlier lot wired: undo/redo as
new revisions (``src/creator/ops/undo.py``, built by WP12 but never given
an HTTP path — see ``<WP13>_wiring.md`` for the exact gap this fills).

Same conventions as ``routes/creator_routes.py`` (WP02): thin HTTP adapter
over ``src/creator/*``, ``creator_enabled`` checked before any store
access, owner always from the authenticated session, "not yours" and
"doesn't exist" both 404.

NOT YET WIRED into ``app.py`` — the report lists the one
``app.include_router(...)`` line that exposes it.
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
    if isinstance(exc, DocumentNotFound):
        raise HTTPException(404, "document not found")
    if isinstance(exc, RevisionConflict):
        raise HTTPException(409, {"reason": "revision_conflict", "current_revision": exc.current_revision})
    if isinstance(exc, (InvalidDocument, InvalidOperation)):
        raise HTTPException(400, str(exc))
    raise


def _get_timeline_doc(owner: str, doc_id: str):
    from src.creator.store import get_store
    doc = get_store().get(owner, doc_id)
    if doc is None:
        raise HTTPException(404, "document not found")
    if doc.kind != "timeline":
        raise HTTPException(400, f"document {doc_id!r} is not a timeline document (kind={doc.kind!r})")
    return doc


def setup_creator_timeline_routes() -> APIRouter:
    router = APIRouter(prefix="/api/creator", tags=["creator-timeline"])

    @router.get("/documents/{doc_id}/timeline/view")
    def get_timeline_view(request: Request, doc_id: str):
        owner = _owner(request)
        _require_flag()
        doc = _get_timeline_doc(owner, doc_id)
        from src.creator.timeline import project_view
        return {"revision": doc.revision, "view": project_view.project_view(doc.content)}

    @router.get("/documents/{doc_id}/timeline/validate")
    def get_timeline_validate(request: Request, doc_id: str):
        owner = _owner(request)
        _require_flag()
        doc = _get_timeline_doc(owner, doc_id)
        from src.creator.timeline import validate as validate_mod
        report = validate_mod.validate(doc.content)
        return {"revision": doc.revision, **report.to_dict()}

    @router.get("/documents/{doc_id}/timeline/edl")
    def get_timeline_edl(request: Request, doc_id: str, track_id: str = ""):
        owner = _owner(request)
        _require_flag()
        doc = _get_timeline_doc(owner, doc_id)
        from src.creator.errors import InvalidOperation
        from src.creator.timeline import project_view
        try:
            edl = project_view.to_edl(doc.content, track_id=track_id or None, title=doc_id)
        except InvalidOperation as exc:
            raise HTTPException(400, str(exc))
        return Response(content=edl, media_type="text/plain")

    @router.get("/documents/{doc_id}/revisions/{revision}")
    def get_revision_snapshot(request: Request, doc_id: str, revision: int):
        owner = _owner(request)
        _require_flag()
        from src.creator.store import get_store
        snap = get_store().get_revision_snapshot(owner, doc_id, revision)
        if snap is None:
            raise HTTPException(404, "revision not found")
        return snap

    @router.post("/documents/{doc_id}/undo")
    async def undo_document(request: Request, doc_id: str, response: Response):
        """Restores an earlier revision's CONTENT as a brand new revision
        (``src/creator/ops/undo.py::undo_to``) — never rewrites history.
        "Redo" is the same call pointed at a later ``target_revision``."""
        owner = _owner(request)
        _require_flag()
        payload = await _json_object(request)
        command_id = str(payload.get("command_id") or "")
        if not command_id:
            raise HTTPException(400, "command_id is required")
        if "target_revision" not in payload:
            raise HTTPException(400, "target_revision is required")
        if "expected_revision" not in payload:
            raise HTTPException(400, "expected_revision is required")
        try:
            target_revision = int(payload.get("target_revision"))
            expected_revision = int(payload.get("expected_revision"))
        except (TypeError, ValueError):
            raise HTTPException(400, "target_revision/expected_revision must be integers")

        from src.creator.ops.undo import undo_to
        from src.creator.store import get_store
        try:
            result = undo_to(
                get_store(), owner, doc_id, target_revision, command_id,
                expected_revision=expected_revision,
            )
        except Exception as exc:  # narrowed by _doc_error
            _doc_error(exc)
            raise
        response.headers["ETag"] = str(result["doc"].revision)
        return {
            "document": result["doc"].to_public_dict(),
            "applied": result["applied"],
            "deduped": result["deduped"],
        }

    return router
