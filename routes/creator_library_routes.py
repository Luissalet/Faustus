"""Creator library + lineage routes (WP03), over ``src.creator.library`` /
``src.creator.lineage``.

Same pattern as ``routes/creator_routes.py`` (WP02): every route is gated by
the ``creator_enabled`` setting, checked BEFORE any store access; owner is
always resolved from the authenticated session, never from the request body
(CONTRATO.md rule 3); a foreign owner's occurrence answers exactly like a
missing one, 404.

NOT YET WIRED into ``app.py`` — see ``WP03_wiring.md`` for the one
``app.include_router(...)`` line that exposes it.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from src.auth_helpers import require_user


def _owner(request: Request) -> str:
    from core import middleware as mw
    from src.owner_identity import effective_storage_owner
    user = require_user(request)
    return effective_storage_owner(user, auth_is_disabled=mw.auth_disabled()) or ""


def _require_flag() -> None:
    from src.settings import get_setting
    if not bool(get_setting("creator_enabled", False)):
        raise HTTPException(status_code=404, detail="creator is not enabled")


def setup_creator_library_routes() -> APIRouter:
    router = APIRouter(prefix="/api/creator/library", tags=["creator-library"])

    @router.get("")
    def list_library(request: Request, project_id: str = "", kind: str = "",
                     tag: str = "", q: str = ""):
        owner = _owner(request)
        _require_flag()
        if not project_id:
            raise HTTPException(400, "project_id is required")
        from src.creator import library
        try:
            items = library.list_items(owner, project_id, kind=kind, tag=tag, q=q)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return {"project_id": project_id, "items": items}

    @router.get("/export-manifest")
    def export_manifest_route(request: Request, project_id: str = ""):
        owner = _owner(request)
        _require_flag()
        if not project_id:
            raise HTTPException(400, "project_id is required")
        from src.creator import library
        try:
            return library.export_manifest(owner, project_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @router.get("/{occurrence_id}/lineage")
    def lineage_route(request: Request, occurrence_id: str):
        owner = _owner(request)
        _require_flag()
        from src.creator import lineage as lineage_mod
        try:
            ancestor_edges = lineage_mod.ancestors(owner, occurrence_id)
        except lineage_mod.LineageItemNotFound:
            raise HTTPException(404, "artifact not found")

        from src import artifact_identity as identity
        try:
            occ = identity.for_owner(occurrence_id, owner=owner)
        except (identity.ArtifactNotFound, identity.NotTheOwner):
            raise HTTPException(404, "artifact not found")
        descendant_edges = (lineage_mod.descendants(owner, occ.project_id, occurrence_id)
                            if occ.project_id else [])
        return {"occurrence_id": occurrence_id, "ancestors": ancestor_edges,
                "descendants": descendant_edges}

    @router.post("/{occurrence_id}/proxy")
    def build_proxy_route(request: Request, occurrence_id: str):
        owner = _owner(request)
        _require_flag()
        from src.creator import library
        try:
            return library.generate_proxy(owner, occurrence_id)
        except library.LibraryItemNotFound:
            raise HTTPException(404, "artifact not found")
        except library.ProxyUnavailable as exc:
            raise HTTPException(409, str(exc))
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    return router
