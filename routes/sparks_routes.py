"""The DGX Spark cluster for Studio, proxied from Prometheus's Hoard (src/sparks.py).

`GET /api/sparks/status` feeds the header pill and Settings → Sparks; a Prometheus that is closed answers 200 with
``{"ok": false, "error": "prometheus unreachable"}`` and the pill hides itself. Loading or unloading a recipe, syncing and changing
the preferences act on shared machines, so they are admin acts.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from core.middleware import require_admin
from src import sparks


class DeployBody(BaseModel):
    recipe: str = Field(..., min_length=1, max_length=64)
    action: str = Field(..., pattern="^(start|stop)$")
    stop_conflicts: bool = False


class PrefsBody(BaseModel):
    enabled: Optional[bool] = None
    url: Optional[str] = Field(default=None, max_length=300)
    default_backend: Optional[bool] = None
    recipe: Optional[str] = Field(default=None, max_length=64)
    first_token_timeout_s: Optional[float] = Field(default=None, ge=5, le=120)


def setup_sparks_routes() -> APIRouter:
    router = APIRouter(tags=["sparks"])

    @router.get("/api/sparks/status")
    async def get_status(recipes: bool = True) -> Dict[str, Any]:
        return await asyncio.to_thread(sparks.status, with_recipes=recipes)

    @router.post("/api/sparks/sync")
    async def post_sync(request: Request) -> Dict[str, Any]:
        require_admin(request)
        return await asyncio.to_thread(sparks.sync)

    @router.post("/api/sparks/deploy")
    async def post_deploy(body: DeployBody, request: Request) -> Dict[str, Any]:
        require_admin(request)
        return await asyncio.to_thread(sparks.deploy, body.recipe, body.action, stop_conflicts=body.stop_conflicts)

    @router.put("/api/sparks/settings")
    async def put_settings(body: PrefsBody, request: Request) -> Dict[str, Any]:
        require_admin(request)
        return await asyncio.to_thread(sparks.set_preferences, enabled=body.enabled, url=body.url,
                                       default_backend=body.default_backend, recipe=body.recipe,
                                       first_token_timeout_s=body.first_token_timeout_s)

    return router
