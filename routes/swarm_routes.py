"""routes/swarm_routes.py — `/api/swarm/*` (src/swarm/, swarm map).

  GET  /api/swarm                          this owner's recent runs
  GET  /api/swarm/{run_id}                 one run's progress
  GET  /api/swarm/{run_id}/results         result rows, paged (?offset&limit&status)
  POST /api/swarm/{run_id}/cancel          stop it; finished items are kept
  POST /api/swarm/{run_id}/resume          run its pending items again
  GET  /api/swarm/{run_id}/files/{name}    one of its exported table files

Runs are started by the `swarm_map` tool. Every route is owner-scoped: another
owner's run answers 404, exactly like a run that does not exist.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse

from src.auth_helpers import effective_user, require_user
from src.swarm import service as _service
from src.swarm.store import SwarmNotFoundError

logger = logging.getLogger(__name__)

_MEDIA = {".md": "text/markdown; charset=utf-8", ".csv": "text/csv; charset=utf-8",
          ".jsonl": "application/x-ndjson; charset=utf-8"}


def _owner(request: Request) -> str:
    return str(effective_user(request) or "")


def _not_found(exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=404, content={"error_class": "swarm.not_found", "detail": str(exc)})


def setup_swarm_routes() -> APIRouter:
    router = APIRouter(prefix="/api/swarm", tags=["swarm"])

    @router.get("")
    def list_runs(request: Request, limit: int = 50, _u: str = Depends(require_user)) -> Any:
        return {"runs": _service.list_runs(_owner(request), limit=max(1, min(200, limit)))}

    @router.get("/{run_id}")
    def get_run(run_id: str, request: Request, _u: str = Depends(require_user)) -> Any:
        try:
            return _service.status(run_id, _owner(request))
        except SwarmNotFoundError as exc:
            return _not_found(exc)

    @router.get("/{run_id}/results")
    def get_results(run_id: str, request: Request, offset: int = 0, limit: int = 50,
                    status: str = "", _u: str = Depends(require_user)) -> Any:
        try:
            return _service.results(run_id, _owner(request), offset=offset, limit=limit,
                                    status_filter=status)
        except SwarmNotFoundError as exc:
            return _not_found(exc)

    @router.post("/{run_id}/cancel")
    def cancel_run(run_id: str, request: Request, _u: str = Depends(require_user)) -> Any:
        try:
            return _service.cancel(run_id, _owner(request))
        except SwarmNotFoundError as exc:
            return _not_found(exc)

    @router.post("/{run_id}/resume")
    async def resume_run(run_id: str, request: Request, _u: str = Depends(require_user)) -> Any:
        try:
            return _service.resume(run_id, _owner(request))
        except SwarmNotFoundError as exc:
            return _not_found(exc)

    @router.get("/{run_id}/files/{name}")
    def get_file(run_id: str, name: str, request: Request, _u: str = Depends(require_user)) -> Any:
        try:
            path = _service.export_path(run_id, _owner(request), name)
        except SwarmNotFoundError as exc:
            return _not_found(exc)
        ext = "." + name.rsplit(".", 1)[-1] if "." in name else ""
        return FileResponse(path, media_type=_MEDIA.get(ext, "application/octet-stream"), filename=name)

    return router
