"""CMP-06 (INFORME_COMPARATIVO_V2.md §3.5) — /api/external-runtimes/herdr/*.

Thin HTTP wrapper over `src/external_runtimes/herdr.py`, a READ-ONLY
adapter: nothing under this prefix ever sends a session, an input or a
command to Herdr. Connection config (`base_url`/`token`) is admin-only to
read (masked — the token is never returned) and write, the same posture
`routes/model_router_routes.py` uses for its config surface; `/version` and
`/sessions` are any signed-in user (`effective_user`), same as
`routes/attention_routes.py`.

Error convention: flat `{"error_class": ..., "detail": ...}`
(`routes/git_routes.py::_error`, named by the lote contract explicitly).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from core.middleware import require_admin
from src import external_runtimes
from src.auth_helpers import effective_user

logger = logging.getLogger(__name__)


def _error(status: int, error_class: str, detail: Optional[str] = None, **extra: Any) -> JSONResponse:
    body: Dict[str, Any] = {"error_class": error_class}
    if detail is not None:
        body["detail"] = detail
    body.update(extra)
    return JSONResponse(status_code=status, content=body)


class HerdrConfigPatch(BaseModel):
    base_url: str
    # Omitted -> keep the stored token unchanged; "" -> clear it explicitly.
    token: Optional[str] = None


def setup_external_runtimes_routes() -> APIRouter:
    router = APIRouter(prefix="/api/external-runtimes", tags=["external-runtimes"])

    @router.get("/herdr/config")
    async def get_herdr_config(request: Request) -> Dict[str, Any]:
        require_admin(request)
        return external_runtimes.load_config().to_dict(include_token=True)

    @router.put("/herdr/config")
    async def put_herdr_config(request: Request, body: HerdrConfigPatch) -> Dict[str, Any]:
        require_admin(request)
        cfg = external_runtimes.save_config(base_url=body.base_url, token=body.token)
        return cfg.to_dict(include_token=True)

    @router.get("/herdr/version")
    async def get_herdr_version(request: Request) -> Any:
        effective_user(request)  # any signed-in user may read status
        client = external_runtimes.HerdrClient(external_runtimes.load_config())
        try:
            return client.negotiate_version()
        except external_runtimes.NotConfiguredError as exc:
            return _error(409, exc.code, exc.message)
        except external_runtimes.UnsupportedVersionError as exc:
            return _error(409, exc.code, exc.message)
        except external_runtimes.TransportError as exc:
            return _error(502, exc.code, exc.message, delivery=exc.delivery)
        except external_runtimes.HerdrError as exc:
            return _error(502, exc.code, exc.message)

    @router.get("/herdr/sessions")
    async def get_herdr_sessions(request: Request) -> Dict[str, Any]:
        effective_user(request)
        client = external_runtimes.HerdrClient(external_runtimes.load_config())
        try:
            rows = client.list_presence()
        except external_runtimes.NotConfiguredError as exc:
            return _error(409, exc.code, exc.message)
        except external_runtimes.TransportError as exc:
            return _error(502, exc.code, exc.message, delivery=exc.delivery)
        except external_runtimes.HerdrError as exc:
            return _error(502, exc.code, exc.message)
        return {"sessions": [row.to_dict() for row in rows], "count": len(rows)}

    return router
