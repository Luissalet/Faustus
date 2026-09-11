"""MOD-05 admin surface -- /api/model-router/* (FAUSTUS).

Thin HTTP wrapper over `src/model_router.py`: config is admin-only to read
and write (it decides whether a turn is EVER allowed to escalate to a paid
model), `preview` runs `choose()` without touching any session state so an
operator or the frontend can see what the router would do before turning it
on, and `log`/`stats` are read-only receipts of what it has actually done.

Error convention: a flat `{"error_class": ..., "detail": ...}` body on
config validation failures, same shape as `routes/git_routes.py::_error`
(this lote's contract names that function explicitly as the convention to
follow).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core.middleware import require_admin
from src import model_router

logger = logging.getLogger(__name__)


def _error(status: int, error_class: str, detail: Optional[str] = None, **extra: Any) -> JSONResponse:
    body: Dict[str, Any] = {"error_class": error_class}
    if detail is not None:
        body["detail"] = detail
    body.update(extra)
    return JSONResponse(status_code=status, content=body)


class ConfigPatch(BaseModel):
    """Every field optional -- only keys present in the body are changed
    (`model_router.update_router_config` merges onto the stored config)."""

    enabled: Optional[bool] = None
    prefer_local: Optional[bool] = None
    max_latency_s: Optional[float] = None
    allow_paid_escalation: Optional[bool] = None
    candidates: Optional[List[str]] = None
    min_capabilities: Optional[List[str]] = None

    def as_patch(self) -> Dict[str, Any]:
        return {k: v for k, v in self.model_dump().items() if v is not None}


class PreviewRequest(BaseModel):
    requirements: Dict[str, Any] = Field(default_factory=dict)
    installed: List[str] = Field(default_factory=list)


def setup_model_router_routes() -> APIRouter:
    router = APIRouter(prefix="/api/model-router", tags=["model-router"])

    @router.get("/config")
    async def get_config(request: Request) -> Dict[str, Any]:
        require_admin(request)
        return model_router.get_router_config().to_dict()

    @router.put("/config")
    async def put_config(request: Request, body: ConfigPatch):
        require_admin(request)
        try:
            cfg = model_router.update_router_config(body.as_patch())
        except ValueError as exc:
            return _error(400, "model_router.invalid_config", str(exc))
        return cfg.to_dict()

    @router.post("/preview")
    async def preview(request: Request, body: PreviewRequest) -> Dict[str, Any]:
        """Runs `choose()` with `log=False` -- a preview must never pollute
        `model_router_log.jsonl` with a decision nobody actually acted on."""
        require_admin(request)
        requirements = model_router.Requirements.from_dict(body.requirements)
        config = model_router.get_router_config()
        decision = model_router.choose(
            requirements,
            installed=body.installed,
            config=config,
            log=False,
        )
        return {"decision": decision.to_dict(), "explain": model_router.explain(decision)}

    @router.get("/log")
    async def get_log(request: Request, limit: int = 50) -> Dict[str, Any]:
        require_admin(request)
        limit = max(1, min(int(limit or 50), 2000))
        return {"entries": model_router.read_log(limit=limit)}

    @router.get("/stats")
    async def get_stats(request: Request) -> Dict[str, Any]:
        require_admin(request)
        return {"stats": model_router.read_stats()}

    return router
