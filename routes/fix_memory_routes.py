"""routes/fix_memory_routes.py — REST API for Lot D's fix memory.

Owner-scoped like `routes/instincts_routes.py` (same `get_current_user`
helper) — NOT admin-only. See `src/fix_memory.py` for the actual store; this
module is a thin HTTP adapter over it.

    GET    /api/fix-memory?workspace=&project_id=&q=&k=
    GET    /api/fix-memory/stats?workspace=&project_id=
    DELETE /api/fix-memory/{id}

`workspace`/`project_id` resolve to the same `project_key` scoping every
other per-project store in this codebase uses (`src.fix_memory.project_key`,
mirroring `src.instincts.project_key`).
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from src import fix_memory
from src.auth_helpers import get_current_user

logger = logging.getLogger(__name__)


def setup_fix_memory_routes() -> APIRouter:
    router = APIRouter(prefix="/api/fix-memory", tags=["fix_memory"])

    def _owner(request: Request) -> Optional[str]:
        return get_current_user(request)

    # ── literal sub-path first, so it never loses to /{id} ──────────────

    @router.get("/stats")
    async def get_stats(request: Request, workspace: Optional[str] = None,
                         project_id: Optional[str] = None):
        project = fix_memory.project_key(workspace, project_id)
        return fix_memory.stats(_owner(request), project)

    # ── collection ────────────────────────────────────────────────────────

    @router.get("")
    async def list_fixes(request: Request, workspace: Optional[str] = None,
                          project_id: Optional[str] = None, q: str = "",
                          k: int = 20):
        project = fix_memory.project_key(workspace, project_id)
        items = fix_memory.recall(_owner(request), project, q, k=k)
        return {"fixes": items, "count": len(items), "project": project}

    # ── single entry ─────────────────────────────────────────────────────

    @router.delete("/{id}")
    async def delete_fix(request: Request, id: str):
        if not fix_memory.forget(_owner(request), id):
            raise HTTPException(404, "Fix not found")
        return {"deleted": True, "id": id}

    return router
