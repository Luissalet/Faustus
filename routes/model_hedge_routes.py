"""routes/model_hedge_routes.py - hedged first token (src/model_hedge.py).

GET   /api/model-hedge         -- enabled, delay, target, per-model delays, the
                                  hedge the default model would get now, the
                                  selectable targets, recent races and totals.
PATCH /api/model-hedge         -- change {enabled, delay_s, target,
POST  /api/model-hedge            delay_overrides, reissue_max,
                                  reissue_overrides}; any subset. Same effect
                                  as the ``agent_hedge_*`` settings. target
                                  ``reissue`` re-sends the same request to the
                                  same server.
"""
import logging
from typing import Any, Dict, Optional, Union

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from core.middleware import require_admin

logger = logging.getLogger(__name__)


class HedgePatch(BaseModel):
    enabled: Optional[bool] = None
    delay_s: Optional[float] = None
    target: Optional[str] = None
    delay_overrides: Optional[Union[Dict[str, Any], str]] = None
    reissue_max: Optional[float] = None
    reissue_overrides: Optional[Union[Dict[str, Any], str]] = None


def setup_model_hedge_routes():
    router = APIRouter(prefix="/api/model-hedge")

    @router.get("")
    def get_status(request: Request, limit: int = 20):
        require_admin(request)
        from src import model_hedge
        return model_hedge.status(limit=limit)

    def _apply(body: HedgePatch, request: Request):
        require_admin(request)
        from src import model_hedge
        from src.settings import SettingsError
        try:
            return model_hedge.update(body.model_dump(exclude_none=True))
        except (ValueError, SettingsError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @router.patch("")
    def patch_hedge(body: HedgePatch, request: Request):
        return _apply(body, request)

    @router.post("")
    def post_hedge(body: HedgePatch, request: Request):
        return _apply(body, request)

    return router
