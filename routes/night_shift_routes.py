"""Night shift API -- /api/night-shift/* (src/night_shift.py).

  POST /api/night-shift              {tasks, workspace, budget?, model?, verify?}
                                     -> the queued shift
  GET  /api/night-shift              recent shifts for this owner
  GET  /api/night-shift/{id}         one shift's current state + results
  POST /api/night-shift/{id}/stop    ask a running shift to stop after its
                                     current task
  GET  /api/night-shift/{id}/report  the Markdown report

Admin-only: a shift dispatches real workers on this machine, the same trust
class as POST /api/dispatch (routes/dispatch_routes.py).
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

from src import night_shift
from src.auth_helpers import require_user

logger = logging.getLogger(__name__)


def _is_admin(owner: str) -> bool:
    try:
        from src import tool_security as ts
        return bool(ts.owner_is_admin_or_single_user(owner or None))
    except Exception:  # pragma: no cover
        return False


def _owner(request: Request) -> str:
    owner = require_user(request)
    if not _is_admin(owner):
        raise HTTPException(403, "night shift dispatches workers on this machine: admins only")
    return owner


async def _body(request: Request) -> Dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "a JSON body is required")
    if not isinstance(body, dict):
        raise HTTPException(400, "the body must be a JSON object")
    return body


def setup_night_shift_routes() -> APIRouter:
    router = APIRouter(prefix="/api/night-shift", tags=["night-shift"])

    @router.post("")
    async def create(request: Request):
        owner = _owner(request)
        body = await _body(request)
        try:
            shift = night_shift.start(owner, body)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return shift

    @router.get("")
    async def index(request: Request, limit: int = 20):
        owner = _owner(request)
        return {"shifts": night_shift.list_for(owner, limit=max(1, min(int(limit or 20), 100)))}

    def _get(request: Request, shift_id: str) -> Dict[str, Any]:
        owner = _owner(request)
        shift = night_shift.get(owner, shift_id)
        if shift is None:
            raise HTTPException(404, "no such night shift")
        return shift

    @router.get("/{shift_id}")
    async def status(request: Request, shift_id: str):
        return _get(request, shift_id)

    @router.post("/{shift_id}/stop")
    async def stop(request: Request, shift_id: str):
        owner = _owner(request)
        ok = night_shift.stop(owner, shift_id)
        if not ok and night_shift.get(owner, shift_id) is None:
            raise HTTPException(404, "no such night shift")
        return {"id": shift_id, "stopped": ok}

    @router.get("/{shift_id}/report")
    async def report(request: Request, shift_id: str):
        owner = _owner(request)
        if night_shift.get(owner, shift_id) is None:
            raise HTTPException(404, "no such night shift")
        return PlainTextResponse(night_shift.report(owner, shift_id), media_type="text/markdown")

    return router
