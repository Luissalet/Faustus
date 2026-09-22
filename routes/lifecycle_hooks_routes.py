"""routes/lifecycle_hooks_routes.py — admin API for lifecycle hooks
(src/lifecycle_hooks.py).

    GET  /api/lifecycle-hooks                    -> {hooks, presets, events, actions}
    PUT  /api/lifecycle-hooks                     -> {hooks: [...]}   (replaces the whole list)
    POST /api/lifecycle-hooks/presets/{preset_id} -> add preset (idempotent), returns the list
    POST /api/lifecycle-hooks/test                -> {event, ctx, run?} -> dry evaluation or run
    GET  /api/lifecycle-hooks/log                 -> {results: [...]}

Admin-only, same gate as routes/tool_arg_policy_routes.py:
`core.middleware.require_admin`.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from core.middleware import require_admin
from src.lifecycle_hooks import (
    ACTIONS,
    EVENTS,
    PRESETS,
    HookError,
    apply_preset,
    evaluate,
    recent,
    run,
    validate_hooks,
)
from src.settings import SettingsError, get_setting, update_settings

logger = logging.getLogger(__name__)


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": message})


class SaveHooksRequest(BaseModel):
    hooks: List[Dict[str, Any]]


class TestHooksRequest(BaseModel):
    event: str
    ctx: Dict[str, Any] = {}
    run: bool = False


def setup_lifecycle_hooks_routes() -> APIRouter:
    router = APIRouter(tags=["lifecycle-hooks"])

    @router.get("/api/lifecycle-hooks")
    async def get_lifecycle_hooks(request: Request) -> Dict[str, Any]:
        require_admin(request)
        return {
            "hooks": get_setting("lifecycle_hooks", []) or [],
            "presets": [{"preset_id": p["preset_id"], "description": p["description"]} for p in PRESETS],
            "events": list(EVENTS),
            "actions": list(ACTIONS),
        }

    @router.put("/api/lifecycle-hooks")
    async def put_lifecycle_hooks(request: Request, body: SaveHooksRequest):
        require_admin(request)
        try:
            checked = validate_hooks(body.hooks)
        except HookError as exc:
            return _error(400, str(exc))
        try:
            update_settings({"lifecycle_hooks": checked})
        except SettingsError as exc:
            return _error(400, str(exc))
        return {"hooks": checked}

    @router.post("/api/lifecycle-hooks/presets/{preset_id}")
    async def add_lifecycle_hooks_preset(request: Request, preset_id: str):
        require_admin(request)
        existing = get_setting("lifecycle_hooks", []) or []
        try:
            updated = apply_preset(existing, preset_id)
        except HookError as exc:
            return _error(400, str(exc))
        try:
            update_settings({"lifecycle_hooks": updated})
        except SettingsError as exc:
            return _error(400, str(exc))
        return {"hooks": updated}

    @router.post("/api/lifecycle-hooks/test")
    async def test_lifecycle_hooks(request: Request, body: TestHooksRequest) -> Dict[str, Any]:
        require_admin(request)
        event = (body.event or "").strip()
        if event not in EVENTS:
            return _error(400, f"event must be one of {list(EVENTS)}")
        ctx = body.ctx if isinstance(body.ctx, dict) else {}
        if not body.run:
            matched = evaluate(event, ctx)
            return {"matched": [h["id"] for h in matched]}
        results = run(event, ctx)
        return {"results": [r.as_dict() for r in results]}

    @router.get("/api/lifecycle-hooks/log")
    async def get_lifecycle_hooks_log(request: Request, limit: int = 50) -> Dict[str, Any]:
        require_admin(request)
        return {"results": recent(limit=limit)}

    return router
