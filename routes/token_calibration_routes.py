"""routes/token_calibration_routes.py — admin API for src/token_calibration.py.

GET /api/token-calibration -> per-model {factor, samples, ratio_ema, last_update}

Auth follows the same seam as routes/admin_wipe/admin_wipe_routes.py
(``require_admin``): this exposes internal calibration state across every
session/user, not one caller's own data, so it gets the admin gate rather
than the per-session ``require_user`` + ownership check other routes use.
"""
from __future__ import annotations

from fastapi import APIRouter, Request

from core.middleware import require_admin
from src import token_calibration


def setup_token_calibration_routes() -> APIRouter:
    router = APIRouter(prefix="/api", tags=["token-calibration"])

    @router.get("/token-calibration")
    def get_token_calibration(request: Request):
        require_admin(request)
        return {"models": token_calibration.stats()}

    return router
