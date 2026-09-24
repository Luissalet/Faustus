"""routes/bug_hunt_routes.py — admin API for the autonomous bug hunter
(src/bug_hunt.py).

    POST /api/bug-hunt            -> runs synchronously (bounded by timeout_s)
                                      and returns the structured report
    GET  /api/bug-hunt/reports    -> the owner's last saved reports

Admin-only, same gate as the other admin/agent routes
(routes/tool_arg_policy_routes.py, routes/agent_settings_routes.py):
`core.middleware.require_admin`.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from core.middleware import require_admin

logger = logging.getLogger(__name__)


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": message})


class BugHuntRequest(BaseModel):
    workspace: str
    target: str
    keep_tests: bool = False
    max_cases: Optional[int] = None
    timeout_s: Optional[int] = None
    model: Optional[str] = None


def _owner(request: Request) -> str:
    try:
        return str(getattr(request.state, "user", None) or request.headers.get("X-User") or "") or "admin"
    except Exception:  # noqa: BLE001
        return "admin"


def setup_bug_hunt_routes() -> APIRouter:
    router = APIRouter(tags=["bug-hunt"])

    @router.post("/api/bug-hunt")
    async def post_bug_hunt(request: Request, body: BugHuntRequest) -> Dict[str, Any]:
        require_admin(request)
        workspace = (body.workspace or "").strip()
        target = (body.target or "").strip()
        if not workspace or not os.path.isdir(workspace):
            return _error(400, "workspace must be an existing directory")
        if not target:
            return _error(400, "target is required")
        from src import bug_hunt

        owner = _owner(request)
        try:
            report = await bug_hunt.hunt(
                workspace, target, owner=owner, keep_tests=bool(body.keep_tests),
                model=body.model, max_cases=body.max_cases, timeout_s=body.timeout_s,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("[bug_hunt] route failed for %r: %s", target, e, exc_info=True)
            return _error(500, str(e)[:400])
        return report.to_dict()

    @router.get("/api/bug-hunt/reports")
    async def get_bug_hunt_reports(request: Request, workspace: str = "") -> Dict[str, Any]:
        require_admin(request)
        from src import bug_hunt

        owner = _owner(request)
        reports = bug_hunt.list_reports(owner)
        if workspace:
            root = os.path.realpath(workspace)
            reports = [r for r in reports if r.get("workspace") == root]
        return {"reports": reports}

    return router
