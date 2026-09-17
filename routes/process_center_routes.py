# routes/process_center_routes.py
"""`/api/process-center` — the control center: what is running because of
Faustus (ports, background jobs, launched profiles, MCP children, watched
apps) and a Stop for each row.

Read routes are `require_admin` like the rest of the technical screens.
**Stop is `require_human`**: it refuses Faustus's own internal tool token,
so the agent cannot reach it — there is deliberately no agent tool that
kills by pid (see `src/process_center.py`). A stop needs the `created_at`
the listing showed, which is the proof the pid still holds the same
process.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from core.middleware import require_admin, require_human
from src import process_center

logger = logging.getLogger(__name__)


class StopBody(BaseModel):
    pid: Optional[int] = None
    created_at: Optional[float] = None
    port: Optional[int] = None
    allow_protected: bool = False


def setup_process_center_routes() -> APIRouter:
    router = APIRouter(prefix="/api/process-center", tags=["process-center"])

    @router.get("")
    async def list_running(request: Request, watched: int = 1) -> Dict[str, Any]:
        require_admin(request)
        try:
            from src import connector_sidecar
            conns = connector_sidecar.list_connectors()
        except Exception:  # noqa: BLE001
            conns = []
        import asyncio
        return await asyncio.to_thread(process_center.snapshot, connectors=conns, include_watched=bool(watched))

    @router.post("/stop")
    async def stop(body: StopBody, request: Request) -> Dict[str, Any]:
        require_human(request)
        import asyncio
        if body.port and not body.pid:
            out = await asyncio.to_thread(process_center.stop_port, int(body.port), allow_protected=body.allow_protected)
        elif body.pid:
            out = await asyncio.to_thread(process_center.stop, int(body.pid), body.created_at, allow_protected=body.allow_protected)
        else:
            raise HTTPException(400, "pid or port is required")
        if not out.get("ok") and out.get("code") in ("self", "os"):
            raise HTTPException(403, out.get("reason") or "refused")
        return out

    return router
