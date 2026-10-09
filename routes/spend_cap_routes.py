"""routes/spend_cap_routes.py - the daily paid-spend cap (src/spend_cap.py).

GET   /api/spend-cap         -- enabled, limits, spent today (USD and tokens),
                                paid calls without a reported cost, when it
                                resets, and the same split per provider.
PATCH /api/spend-cap         -- change {enabled, daily_usd, daily_tokens}; any
POST  /api/spend-cap            subset. Same effect as the three
                                ``paid_spend_cap_*`` settings.

Reading is ``require_admin`` (the agent's own loopback token may look at it);
changing it is ``require_human``: a model must not raise or switch off the cap
that exists to bound it.
"""
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from core.middleware import require_admin, require_human

logger = logging.getLogger(__name__)


class SpendCapPatch(BaseModel):
    enabled: Optional[bool] = None
    daily_usd: Optional[float] = Field(default=None, ge=0, le=1_000_000)
    daily_tokens: Optional[int] = Field(default=None, ge=0, le=10_000_000_000)


def setup_spend_cap_routes():
    router = APIRouter(prefix="/api/spend-cap")

    @router.get("")
    def get_status(request: Request):
        require_admin(request)
        from src import spend_cap
        return spend_cap.status()

    def _apply(body: SpendCapPatch, request: Request):
        require_human(request)
        from src import spend_cap
        from src.settings import SettingsError, update_settings
        patch = {}
        if body.enabled is not None:
            patch[spend_cap.SETTING_ENABLED] = bool(body.enabled)
        if body.daily_usd is not None:
            patch[spend_cap.SETTING_USD] = float(body.daily_usd)
        if body.daily_tokens is not None:
            patch[spend_cap.SETTING_TOKENS] = int(body.daily_tokens)
        if not patch:
            raise HTTPException(status_code=400, detail="nothing to change")
        try:
            update_settings(patch)
        except SettingsError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return spend_cap.status()

    @router.patch("")
    def patch_cap(body: SpendCapPatch, request: Request):
        return _apply(body, request)

    @router.post("")
    def post_cap(body: SpendCapPatch, request: Request):
        return _apply(body, request)

    return router
