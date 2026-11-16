"""Delegation routes API — /api/delegation-routes (src/delegation_routes.py).

  GET /api/delegation-routes   {"routes", "default", "schema"}: the list, the
                               default, and the exact `route` property the
                               model is shown (null with no routes)
  PUT /api/delegation-routes   {"routes": [...], "default"?: name} — validated
                               as a whole; a bad route saves nothing (400)
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request

from core.middleware import require_admin
from src import delegation_routes


def setup_delegation_routes_routes() -> APIRouter:
    router = APIRouter(prefix="/api/delegation-routes", tags=["delegation-routes"])

    def _view(data: Dict[str, Any]) -> Dict[str, Any]:
        return {**data, "schema": delegation_routes.schema_property(data)}

    @router.get("")
    async def get_routes(request: Request) -> Dict[str, Any]:
        require_admin(request)
        return _view(delegation_routes.load())

    @router.put("")
    async def put_routes(request: Request) -> Dict[str, Any]:
        require_admin(request)
        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise HTTPException(400, "a JSON body {routes, default?} is required")
        try:
            data = delegation_routes.save(body.get("routes") or [], str(body.get("default") or ""))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return _view(data)

    return router
