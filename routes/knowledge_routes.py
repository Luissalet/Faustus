"""Knowledge neighborhood API (CMP-04) —
``/api/projects/{project_id}/knowledge/neighborhood``.

One read across requirements, board decisions, the code index and the
requirements' own evidence matrix (``src/knowledge_neighborhood.py``), so a
caller asking "why does this file matter" gets an answer typed
``requirement -> decision -> symbol -> test -> run`` instead of a bag of
search results.

Gating: owner-scoped via ``require_user`` and ``_project_or_404`` (the same
verbatim helper ``routes/requirements_routes.py`` and ``routes/board_routes.py``
already use — a project id that is not this owner's answers exactly like one
that does not exist).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from src.auth_helpers import effective_user, require_user
from src import knowledge_neighborhood
from services.projects import get_store as get_project_store

logger = logging.getLogger(__name__)


def setup_knowledge_routes() -> APIRouter:
    router = APIRouter(prefix="/api/projects/{project_id}/knowledge", tags=["knowledge"])

    def _project_or_404(project_id: str, owner: Optional[str]) -> Dict[str, Any]:
        project = get_project_store().get(project_id, owner)
        if not project:
            raise HTTPException(404, "Project not found")
        return project

    @router.get("/neighborhood")
    def get_neighborhood(
        project_id: str, request: Request,
        path: str = Query("", max_length=1024),
        req_key: str = Query("", max_length=32),
        issue_key: str = Query("", max_length=64),
        depth: int = Query(1, ge=1, le=knowledge_neighborhood.MAX_DEPTH),
        _u: str = Depends(require_user),
    ) -> Dict[str, Any]:
        owner = effective_user(request)
        _project_or_404(project_id, owner)
        result = knowledge_neighborhood.neighborhood(
            project_id, owner or "", path=path or None, req_key=req_key or None,
            issue_key=issue_key or None, depth=depth,
        )
        return {"neighborhood": result}

    return router
