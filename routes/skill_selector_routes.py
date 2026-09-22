# routes/skill_selector_routes.py
"""API for the hybrid skill selector (`src/skills_runtime/selector.py`).

    POST /api/skills/selector/explain  -> ranked candidates + per-lane diagnostics
    GET  /api/skills/selector          -> {mode, threshold, weights}
    PUT  /api/skills/selector          -> update those settings (admin)

`explain` is owner-scoped the same way `routes/skills_routes.py` is
(`get_current_user(request)`); the settings GET/PUT pair is shaped like
`routes/tool_arg_policy_routes.py` (admin-only write, `src.settings`
read/write).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core.middleware import require_admin
from src.auth_helpers import get_current_user
from src.settings import SettingsError, get_setting, update_settings
from src.skills_runtime import selector as skill_selector

logger = logging.getLogger(__name__)

_VALID_MODES = ("hybrid", "lexical")
_WEIGHT_KEYS = ("semantic", "lexical", "trigger")


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": message})


class SelectorExplainRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=4000)
    max_items: int = Field(20, ge=1, le=100)


class SelectorSettingsRequest(BaseModel):
    mode: Optional[str] = None
    threshold: Optional[float] = None
    weights: Optional[Dict[str, float]] = None


def _validate_weights(weights: Dict[str, Any]) -> Dict[str, float]:
    if not isinstance(weights, dict):
        raise ValueError("weights must be an object")
    out: Dict[str, float] = {}
    for key in _WEIGHT_KEYS:
        if key not in weights:
            raise ValueError(f"weights is missing '{key}'")
        try:
            v = float(weights[key])
        except (TypeError, ValueError):
            raise ValueError(f"weights['{key}'] must be a number") from None
        if v < 0:
            raise ValueError(f"weights['{key}'] must be >= 0")
        out[key] = v
    if sum(out.values()) <= 0:
        raise ValueError("weights must not all be zero")
    return out


def setup_skill_selector_routes() -> APIRouter:
    router = APIRouter(prefix="/api/skills/selector", tags=["skill-selector"])

    @router.post("/explain")
    async def explain_selector(request: Request, body: SelectorExplainRequest) -> Dict[str, Any]:
        owner = get_current_user(request)
        query = body.query.strip()
        if not query:
            return _error(400, "query is required")
        candidates = skill_selector.explain(query, owner=owner, max_items=body.max_items)
        return {"query": query, "candidates": candidates, "count": len(candidates)}

    @router.get("")
    async def get_selector_settings(request: Request) -> Dict[str, Any]:
        return {
            "mode": get_setting("skill_selector_mode", skill_selector.DEFAULT_MODE),
            "threshold": get_setting("skill_selector_threshold", skill_selector.DEFAULT_THRESHOLD),
            "weights": get_setting("skill_selector_weights", skill_selector.DEFAULT_WEIGHTS),
        }

    @router.put("")
    async def put_selector_settings(request: Request, body: SelectorSettingsRequest):
        require_admin(request)
        updates: Dict[str, Any] = {}

        if body.mode is not None:
            mode = body.mode.strip().lower()
            if mode not in _VALID_MODES:
                return _error(400, f"mode must be one of {_VALID_MODES}")
            updates["skill_selector_mode"] = mode

        if body.threshold is not None:
            if not (0.0 <= body.threshold <= 1.0):
                return _error(400, "threshold must be between 0 and 1")
            updates["skill_selector_threshold"] = float(body.threshold)

        if body.weights is not None:
            try:
                updates["skill_selector_weights"] = _validate_weights(body.weights)
            except ValueError as exc:
                return _error(400, str(exc))

        if not updates:
            return _error(400, "nothing to update")

        try:
            update_settings(updates)
        except SettingsError as exc:
            return _error(400, str(exc))

        return {
            "mode": get_setting("skill_selector_mode", skill_selector.DEFAULT_MODE),
            "threshold": get_setting("skill_selector_threshold", skill_selector.DEFAULT_THRESHOLD),
            "weights": get_setting("skill_selector_weights", skill_selector.DEFAULT_WEIGHTS),
        }

    return router
