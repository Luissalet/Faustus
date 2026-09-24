"""routes/ci_failures_routes.py -- ``GET /api/ci/failures`` (lot C).

Reads which files broke the last (or a named) GitHub Actions run for the
repo behind `workspace`'s own `git remote`. Read-only, so `require_user`
is enough -- the same gating class `routes/git_routes.py` already uses for
its own status/log endpoints.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Query, Request

from src import ci_failures
from src.auth_helpers import require_user

logger = logging.getLogger(__name__)


def setup_ci_failures_routes() -> APIRouter:
    router = APIRouter(prefix="/api/ci", tags=["ci-failures"])

    @router.get("/failures")
    async def get_failures(
        request: Request,
        workspace: str = Query(..., min_length=1),
        branch: Optional[str] = Query(None),
        run_id: Optional[int] = Query(None),
        propose: bool = Query(False),
        _u: str = Depends(require_user),
    ) -> Dict[str, Any]:
        owner = _u or ""
        try:
            analysis = await ci_failures.analyze(workspace, run_id=run_id, branch=branch)
        except ci_failures.CiFailuresError as exc:
            return {"error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            logger.warning("ci_failures route: analyze failed: %s", exc)
            return {"error": str(exc)}

        fixes = None
        if propose:
            try:
                fixes = await ci_failures.propose_fixes(analysis, owner=owner)
            except Exception as exc:  # noqa: BLE001
                logger.debug("ci_failures route: propose_fixes failed: %s", exc)
                fixes = []

        payload: Dict[str, Any] = {
            "owner": analysis.owner,
            "repo": analysis.repo,
            "run": analysis.run,
            "failures": analysis.blocks,
            "summary_md": analysis.summary_md(),
            "from_cache": analysis.from_cache,
        }
        if fixes is not None:
            payload["proposed_fixes"] = fixes
        return payload

    return router
