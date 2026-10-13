"""Spheres of the family hub, proxied for the browser.

`GET /api/hoard/spheres` and `POST /api/hoard/spheres/active {id}` forward to
the Hoard Hub through `src/hoard_hub.py`, so Studio never calls the hub
cross-origin. A hub that is down or refuses answers 200 with
``{"ok": false, "error": ...}``: the header chip hides itself on that, and
nothing else in Faustus depends on the hub being there.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from fastapi.responses import RedirectResponse
from urllib.parse import urlencode
from pydantic import BaseModel, Field

from core.middleware import require_admin
from src import hoard_hub


class ActiveSphere(BaseModel):
    id: str = Field(default="", max_length=64)


def setup_hoard_hub_routes() -> APIRouter:
    router = APIRouter(tags=["hoard-hub"])

    @router.get('/api/hoard/coordination')
    def coordination_status():
        from src.shared_coordination import status
        return status()

    @router.get('/api/hoard/agora/spaces')
    async def coordination_spaces():
        from src.shared_coordination import call
        return await asyncio.to_thread(call, 'hub_agora_spaces')

    @router.get('/api/hoard/agora/rooms')
    async def coordination_rooms(request: Request, space_id: str = 'ecosystem'):
        from src.shared_coordination import call
        from routes.council_routes import _require_owner
        from routes.group_routes import group_owner
        return await asyncio.to_thread(call, 'hub_coordination_rooms', {'space_id': space_id,
            'owner': _require_owner(request), 'group_owner': group_owner(request) or ''})

    @router.post('/api/hoard/agora/spaces')
    async def create_coordination_space(request: Request):
        require_admin(request)
        from src.shared_coordination import call
        body = await request.json()
        return await asyncio.to_thread(call, 'hub_agora_space_create', {'agent': 'faustus', 'title': body.get('title', '')})

    @router.get('/api/hoard/agora/open')
    def open_coordination_board(space_id: str = 'ecosystem'):
        where = hoard_hub.hub_location()
        return RedirectResponse(where['url'].rstrip('/') + '/agora/?' + urlencode({'space_id': space_id}))

    @router.get("/api/hoard/spheres")
    async def get_spheres() -> Dict[str, Any]:
        return await asyncio.to_thread(hoard_hub.get_spheres)

    @router.get("/api/hoard/fleet")
    async def get_fleet(request: Request) -> Dict[str, Any]:
        # Fleet labels span the machine, including other users' scheduled tasks.
        require_admin(request)
        return await asyncio.to_thread(hoard_hub.get_fleet)

    @router.get("/api/family/fleet")
    def publish_fleet(request: Request):
        # The already configured Hub credential authenticates the family observer;
        # ordinary authenticated administrators can read the same published metadata.
        # AuthMiddleware lets exactly this GET through when it carries the Hub's token; the handler checks it
        # again, so a request that reaches here without it still needs an administrator session.
        if not hoard_hub.hub_bearer_matches(request.headers.get("Authorization", "")):
            require_admin(request)
        from core.database import SessionLocal
        from src.fleet_status import build
        with SessionLocal() as db:
            return build(db)

    @router.post("/api/hoard/spheres/active")
    async def set_active_sphere(body: ActiveSphere, request: Request):
        # The sphere is machine-wide state shared by every app in the family,
        # so switching it is an admin act, like the other routes that act on
        # a plugin app.
        require_admin(request)
        result = await asyncio.to_thread(hoard_hub.set_active, body.id)
        if result.get("status") == 400:
            return JSONResponse(status_code=400, content={"ok": False, "error": result.get("error", "bad request")})
        if result.get("status") == 404:
            return JSONResponse(status_code=404, content={"ok": False, "error": result.get("error", "unknown sphere")})
        result.pop("status", None)
        return result

    return router
