"""routes/staged_review_routes.py — API for the staged worktree review
(src/staged_review.py): cheap deterministic stages (diff, static analysis,
related tests) run before the model and bound what it looks at.

    POST /api/review/worktree

Admin only: the stages run the project's own tooling in that folder (its
linters' configs, its tests), which is running its code. `workspace` must be
an absolute, existing directory. There is no repo
registry (like `routes/git_routes.py`'s `find_repo_meta`) for an arbitrary
worktree path, so — same rule `routes/bug_hunt_routes.py` uses for the same
kind of "run tools against this path" request — ownership is the resolved
caller identity plus an existing-directory check, logged for audit; nothing
here proves ahead of time that the caller "owns" the path.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from core.middleware import require_admin
from src.auth_helpers import effective_user, require_user

logger = logging.getLogger(__name__)


def _error(status: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": detail})


class StagedReviewBody(BaseModel):
    workspace: str
    base: Optional[str] = "HEAD"
    paths: Optional[List[str]] = None
    request: Optional[str] = ""
    #: A model served by one of the owner's endpoints; empty = the utility
    #: model; "none" = no model stage. Never a URL from the request body:
    #: the diff only goes to endpoints the owner configured.
    model: Optional[str] = None
    stages: Optional[List[str]] = None
    timeout_s: Optional[float] = None


def setup_staged_review_routes(session_manager: Any = None) -> APIRouter:
    """`session_manager` is accepted (unused) for the same call shape as the
    other `setup_*_routes(session_manager)` factories `app.py` wires up."""
    router = APIRouter(prefix="/api/review", tags=["review"])

    @router.post("/worktree")
    async def post_review_worktree(request: Request, body: StagedReviewBody,
                                   _u: str = Depends(require_user), _a: None = Depends(require_admin)) -> Any:
        from src import staged_review

        owner = effective_user(request)
        workspace = (body.workspace or "").strip()
        if not workspace or not os.path.isabs(workspace) or not os.path.isdir(workspace):
            return _error(400, "workspace must be an existing absolute directory")

        logger.info("[staged_review] worktree review requested by %r for %r", owner or "?", workspace)

        stages = tuple(s for s in (body.stages or staged_review.STAGES) if s in staged_review.STAGES) \
            or staged_review.STAGES
        notes: List[str] = []
        endpoint_url, model, headers = _resolve_model(owner, (body.model or "").strip()) \
            if "model" in stages else (None, None, None)
        if "model" in stages and not (endpoint_url and model):
            notes.append("model stage skipped: no model endpoint available")

        result: Dict[str, Any] = await staged_review.review_worktree(
            workspace,
            base=(body.base or "HEAD").strip() or "HEAD",
            paths=body.paths or None,
            request=body.request or "",
            endpoint_url=endpoint_url,
            model=model,
            headers=headers,
            stages=stages,
            timeout_s=body.timeout_s,
        )
        if notes:
            result["notes"] = notes
        return result

    return router


def _resolve_model(owner: Optional[str], wanted: str):
    """(endpoint_url, model, headers) from the owner's own endpoints."""
    if wanted.lower() == "none":
        return None, None, None
    try:
        from src import endpoint_resolver as er
        if wanted:
            ep = er.endpoint_id_serving(wanted, owner=owner or None)
            routed = er.resolve_endpoint_by_id(ep, model=wanted, owner=owner or None) if ep else None
            if routed:
                return routed
        routed = er.resolve_endpoint("utility", owner=owner or None)
        return routed if routed and len(routed) == 3 else (None, None, None)
    except Exception as e:  # noqa: BLE001 - no model is a skipped stage, not an error
        logger.info("[staged_review] no model endpoint: %s", e)
        return None, None, None
