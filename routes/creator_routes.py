"""Creator domain routes (WP02): documents + profile, over HTTP.

``src/creator/`` is the sole authority for CreatorDocument/CreatorProfile;
this module is a thin HTTP adapter. Every route is gated by the
``creator_enabled`` setting (default False, ``src/settings.py``), checked
BEFORE any store access — with the flag off every route below answers 404
and nothing is read or written (CONTRATO.md rule 5).

Owner is always resolved from the authenticated session
(``effective_storage_owner``/``require_user``), never from the request body
(CONTRATO.md rule 3). A document or project that exists but belongs to
someone else answers identically to one that does not exist at all: 404.

NOT YET WIRED into ``app.py`` (out of this lot's file list, see
``docs/spec/creator/plan/CONTRATO.md`` rule 11) — the report lists the one
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


def _creator_enabled() -> bool:
    from src.settings import get_setting
    return bool(get_setting("creator_enabled", False))


def _require_flag() -> None:
    if not _creator_enabled():
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
        raise HTTPException(
            409,
            {"reason": "revision_conflict", "current_revision": exc.current_revision},
        )
    if isinstance(exc, (InvalidDocument, InvalidOperation)):
        raise HTTPException(400, str(exc))
    raise


def setup_creator_routes() -> APIRouter:
    router = APIRouter(prefix="/api/creator", tags=["creator"])

    @router.get("/capabilities")
    def get_capabilities(request: Request, project_id: str = ""):
        require_user(request)
        _require_flag()
        from src.creator.documents import DOCUMENT_KINDS, DOCUMENT_STATES
        from src import media_capabilities
        return {
            "creator_enabled": True,
            "project_id": project_id,
            "document_kinds": list(DOCUMENT_KINDS),
            "document_states": list(DOCUMENT_STATES),
            "media_backends": media_capabilities.capabilities_manifest(force=False),
        }

    @router.get("/documents")
    def list_documents(request: Request, project_id: str = ""):
        owner = _owner(request)
        _require_flag()
        if not project_id:
            raise HTTPException(400, "project_id is required")
        from src.creator.store import get_store
        docs = get_store().list_for_project(owner, project_id)
        return {"documents": [d.to_public_dict() for d in docs]}

    @router.post("/documents")
    async def create_document(request: Request):
        owner = _owner(request)
        _require_flag()
        payload = await _json_object(request)
        project_id = str(payload.get("project_id") or "")
        kind = str(payload.get("kind") or "")
        content = payload.get("content")
        state = str(payload.get("state") or "proposed")
        if not project_id:
            raise HTTPException(400, "project_id is required")

        from services.projects import get_store as get_project_store
        if get_project_store().get(project_id, owner) is None:
            raise HTTPException(404, "project not found")

        from src.creator.store import get_store
        from src.creator.errors import InvalidDocument
        try:
            doc = get_store().create(owner, project_id, kind, content, state=state)
        except InvalidDocument as exc:
            raise HTTPException(400, str(exc))
        return doc.to_public_dict()

    @router.get("/documents/{doc_id}")
    def get_document(request: Request, doc_id: str, response: Response):
        owner = _owner(request)
        _require_flag()
        from src.creator.store import get_store
        doc = get_store().get(owner, doc_id)
        if doc is None:
            raise HTTPException(404, "document not found")
        response.headers["ETag"] = str(doc.revision)
        return doc.to_public_dict()

    @router.post("/documents/{doc_id}/commands")
    async def apply_command(request: Request, doc_id: str, response: Response):
        owner = _owner(request)
        _require_flag()
        payload = await _json_object(request)
        command_id = str(payload.get("command_id") or "")
        if not command_id:
            raise HTTPException(400, "command_id is required")
        if "expected_revision" not in payload:
            raise HTTPException(400, "expected_revision is required")
        try:
            expected_revision = int(payload.get("expected_revision"))
        except (TypeError, ValueError):
            raise HTTPException(400, "expected_revision must be an integer")
        op = payload.get("op")
        if not isinstance(op, dict):
            raise HTTPException(400, "op must be an object")

        from src.creator.store import get_store
        try:
            result = get_store().apply_command(owner, doc_id, command_id, expected_revision, op)
        except Exception as exc:  # narrowed by _doc_error
            _doc_error(exc)
            raise
        response.headers["ETag"] = str(result["doc"].revision)
        return {
            "document": result["doc"].to_public_dict(),
            "applied": result["applied"],
            "deduped": result["deduped"],
        }

    @router.get("/documents/{doc_id}/history")
    def get_history(request: Request, doc_id: str):
        owner = _owner(request)
        _require_flag()
        from src.creator.store import get_store
        store = get_store()
        if store.get(owner, doc_id) is None:
            raise HTTPException(404, "document not found")
        return {"commands": store.history(owner, doc_id)}

    @router.get("/profile")
    def get_profile_route(request: Request, project_id: str = ""):
        owner = _owner(request)
        _require_flag()
        if not project_id:
            raise HTTPException(400, "project_id is required")
        from src.creator import profile as profile_mod
        from services.projects import get_store as get_project_store
        result = profile_mod.get_profile(get_project_store(), owner, project_id)
        if result is None:
            raise HTTPException(404, "project not found")
        return {"project_id": project_id, "profile": result}

    @router.put("/profile")
    async def put_profile_route(request: Request, project_id: str = ""):
        owner = _owner(request)
        _require_flag()
        if not project_id:
            raise HTTPException(400, "project_id is required")
        payload = await _json_object(request)
        from src.creator import profile as profile_mod
        from services.projects import get_store as get_project_store
        result = profile_mod.set_profile(get_project_store(), owner, project_id, payload)
        if result is None:
            raise HTTPException(404, "project not found")
        return {"project_id": project_id, "profile": result}

    return router
