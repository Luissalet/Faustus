"""Dispatch API — local workers for an outside coordinator (src/dispatch.py).

  POST /api/dispatch                 {tasks, workspace?, model?, parallel?, reviewer?,
                                      max_rounds?, timeout_s?, context?} → the job
  GET  /api/dispatch                 recent jobs (compact, no result)
  GET  /api/dispatch/{id}            status + compact result (+ progress while running)
  GET  /api/dispatch/{id}/wait?timeout=N   long-poll (≤ 600 s), then the same as GET
  GET  /api/dispatch/{id}/events     the board's events so far (last 400)
  POST /api/dispatch/{id}/cancel

Callers: a signed-in user (cookie), or an API token with the `agents:dispatch`
scope — the token Fable / Claude Desktop / a script uses from outside the
app. Never the model inside a chat: app_api is blocked from this prefix
(delegate_agents is its tool for that, with the chat's own gate).
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request

from src import dispatch
from src.auth_helpers import require_user

logger = logging.getLogger(__name__)

SCOPE = "agents:dispatch"
_MAX_WAIT_S = 600.0


def _owner(request: Request) -> str:
    """Cookie user, or an API token carrying the dispatch scope."""
    if getattr(request.state, "api_token", False):
        scopes = set(getattr(request.state, "api_token_scopes", []) or [])
        if SCOPE not in scopes:
            raise HTTPException(403, f"API token missing required scope: {SCOPE}")
        owner = getattr(request.state, "api_token_owner", None)
        if not owner:
            raise HTTPException(403, "API token has no owner")
        return owner
    return require_user(request)


def _visible(job: dispatch.DispatchJob, owner: str) -> bool:
    # single-user / anonymous modes have owner "" — everything is theirs
    return not owner or job.owner in (owner, None, "")


async def _body(request: Request) -> Dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "a JSON body is required")
    if not isinstance(body, dict):
        raise HTTPException(400, "the body must be a JSON object")
    return body


def setup_dispatch_routes() -> APIRouter:
    router = APIRouter(prefix="/api/dispatch", tags=["dispatch"])

    @router.post("")
    async def create(request: Request):
        owner = _owner(request)
        body = await _body(request)
        try:
            job = await dispatch.start(owner or None, body)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return dispatch.compact(job)

    @router.get("")
    async def index(request: Request, limit: int = 50):
        owner = _owner(request)
        return {"jobs": dispatch.list_jobs(owner or None, limit=max(1, min(int(limit or 50), 200)))}

    def _get(request: Request, job_id: str) -> dispatch.DispatchJob:
        owner = _owner(request)
        job = dispatch.get(job_id)
        if job is None or not _visible(job, owner):
            raise HTTPException(404, "no such dispatch job")
        return job

    @router.get("/guide")
    async def guide(request: Request):
        """How a coordinating model should use the workers — the same text
        the MCP server's `workers_guide` tool returns."""
        _owner(request)
        return {"guide": dispatch.COORDINATOR_GUIDE}

    @router.get("/{job_id}")
    async def status(request: Request, job_id: str):
        return dispatch.compact(_get(request, job_id))

    @router.get("/{job_id}/wait")
    async def wait(request: Request, job_id: str, timeout: float = 120.0):
        job = _get(request, job_id)
        try:
            t = float(timeout)
        except (TypeError, ValueError):
            t = 120.0
        await dispatch.wait(job, max(0.0, min(t, _MAX_WAIT_S)))
        return dispatch.compact(job)

    @router.get("/{job_id}/events")
    async def events(request: Request, job_id: str):
        job = _get(request, job_id)
        return {"id": job.id, "status": job.status, "events": list(job.events)}

    @router.post("/{job_id}/cancel")
    async def cancel(request: Request, job_id: str):
        job = _get(request, job_id)
        return {"id": job.id, "cancelled": dispatch.cancel(job), "status": job.status}

    return router
