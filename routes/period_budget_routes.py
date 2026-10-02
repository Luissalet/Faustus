"""Period budget API - /api/budget/period (src/period_budget.py).

  GET  /api/budget/period                  state per provider and window (used, target, pace line,
                                           paused_until), the local GPU-seconds window, whether an
                                           interactive turn is live, the failure breaker and the
                                           provider cooldowns
  POST /api/budget/period/breaker/reset    close the unattended-failure breaker now
  POST /api/budget/period/cooldowns/clear  {endpoint?} forget one endpoint's cooldown, or all of them

Reading needs a signed-in user or an API token with the `sessions` or
`agents:dispatch` scope (core/authz.py). Resetting changes what unattended
work may start, so it needs an admin signed in (single-user mode counts); an
API token never reaches the reset routes.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request

from src.auth_helpers import _auth_disabled, effective_user, require_user


def _require_admin(request: Request) -> str:
    owner = require_user(request)
    try:
        from src import tool_security as ts
        if not ts.owner_is_admin_or_single_user(owner or None):
            raise HTTPException(403, "only an admin may reset the unattended-work guards")
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001
        raise HTTPException(403, "only an admin may reset the unattended-work guards")
    return owner


def setup_period_budget_routes() -> APIRouter:
    router = APIRouter(prefix="/api/budget/period", tags=["budget"])

    @router.get("")
    async def get_period(request: Request) -> Dict[str, Any]:
        # `effective_user` (not `require_user`): an API token with the right
        # scope has already been admitted by core/authz.py and resolves to its owner.
        if not effective_user(request) and not _auth_disabled():
            raise HTTPException(401, "Authentication required")
        from src import period_budget
        return await asyncio.to_thread(period_budget.state)

    @router.post("/breaker/reset")
    async def reset_breaker(request: Request) -> Dict[str, Any]:
        _require_admin(request)
        from src import unattended_breaker
        return {"breaker": unattended_breaker.reset()}

    @router.post("/cooldowns/clear")
    async def clear_cooldowns(request: Request) -> Dict[str, Any]:
        _require_admin(request)
        endpoint = ""
        try:
            body = await request.json()
            if isinstance(body, dict):
                endpoint = str(body.get("endpoint") or "").strip()
        except Exception:  # noqa: BLE001
            endpoint = ""
        from src import unattended_breaker
        return {"cleared": unattended_breaker.clear_cooldown(endpoint or None),
                "cooldowns": unattended_breaker.cooldowns()}

    return router