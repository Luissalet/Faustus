"""
routes/model_architecture_routes.py — `GET /api/models/architecture`
(INF-01 §A).

A new, small route module rather than another few hundred lines in the
already very large `routes/model_routes.py` (contract's stated alternative;
Lote-style file ownership keeps this change's blast radius to one file).
Registered in `app.py` next to `setup_board_routes()`.

Passive read only: never loads a model, never launches anything. See
`src/model_architecture.py` for the derivation rules and
`docs/api/model_architecture.md` for the response contract.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from src.auth_helpers import get_current_user, require_user
from src.model_architecture import VALID_SOURCES, get_model_architecture


def setup_model_architecture_routes() -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.get("/models/architecture")
    def api_model_architecture(request: Request, repo: str = "", source: str = "auto"):
        # Any signed-in caller may read this — same authorization level as
        # `GET /api/models` — it is a passive metadata lookup, not an action.
        require_user(request)
        repo = (repo or "").strip()
        if not repo:
            raise HTTPException(400, "repo is required")
        src = (source or "auto").strip().lower()
        if src not in VALID_SOURCES:
            raise HTTPException(400, f"source must be one of {', '.join(VALID_SOURCES)}")
        try:
            owner = get_current_user(request) or ""
        except Exception:
            owner = ""
        return get_model_architecture(repo, src, owner=owner)

    return router
