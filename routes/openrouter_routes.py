"""routes/openrouter_routes.py — per-endpoint OpenRouter option prefs
(OBJ-8, Lote A2) — ``/api/openrouter/*``.

Read/write surface over `src/openrouter_options.py`'s persisted store: what
an admin sees and edits (provider routing preferences, opt-in web search,
native fallback) that `src/llm_core.py` then applies to the payload of every
OpenRouter call for that endpoint. Deliberately admin-only, same gate
`routes/privacy_routes.py` uses (`core.middleware.require_admin`) — these
options can change what an OpenRouter call costs and whether it opts out of
provider-side data retention, so they follow the same "an ordinary user's
loopback token cannot touch this" rule as the privacy profile itself.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from core.middleware import require_admin
from src import openrouter_options

logger = logging.getLogger(__name__)


def _error(status: int, error_class: str, detail: Optional[str] = None, **extra: Any) -> JSONResponse:
    """Flat error body — same convention as `routes/git_routes.py::_error`:
    `error_class` sits next to `detail` at the top level, not nested under
    it (a plain `HTTPException(status, {...})` would nest it as
    `{"detail": {...}}`, which `core.middleware`'s OBS-03 error_class
    backfill only fills in when the body doesn't already carry one).
    """
    body: Dict[str, Any] = {"error_class": error_class}
    if detail is not None:
        body["detail"] = detail
    body.update(extra)
    return JSONResponse(status_code=status, content=body)


def setup_openrouter_routes() -> APIRouter:
    router = APIRouter(prefix="/api/openrouter", tags=["openrouter"])

    @router.get("/prefs")
    def get_all_prefs(request: Request) -> Dict[str, Any]:
        require_admin(request)
        return {"endpoints": openrouter_options.all_prefs()}

    @router.get("/endpoints/{endpoint_id}/prefs")
    def get_endpoint_prefs(endpoint_id: str, request: Request) -> Dict[str, Any]:
        require_admin(request)
        return openrouter_options.get_prefs(endpoint_id)

    @router.put("/endpoints/{endpoint_id}/prefs")
    def put_endpoint_prefs(endpoint_id: str, patch: Dict[str, Any], request: Request):
        require_admin(request)
        try:
            return openrouter_options.set_prefs(endpoint_id, patch)
        except ValueError as exc:
            return _error(400, "openrouter.invalid_prefs", str(exc))

    @router.delete("/endpoints/{endpoint_id}/prefs")
    def delete_endpoint_prefs(endpoint_id: str, request: Request) -> Dict[str, Any]:
        require_admin(request)
        openrouter_options.delete_prefs(endpoint_id)
        return {"deleted": True, "endpoint_id": endpoint_id}

    return router
