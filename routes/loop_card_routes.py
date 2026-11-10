"""routes/loop_card_routes.py -- HTTP surface for Loop Cards (src/loop_card.py).

GET    /api/loop-cards[?project=]           the owner's cards, newest first
POST   /api/loop-cards                      {goal, command, extract, direction?, repeats?, cwd?, project?, timeout_s?}  measure and store
GET    /api/loop-cards/{id}                 the whole card (baseline runs, check history)
POST   /api/loop-cards/{id}/check           {command?, repeats?, rebase?}  measure again: win / loss / noise
DELETE /api/loop-cards/{id}
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from core.middleware import require_admin
from src import loop_card
from src.auth_helpers import effective_user


def _reply(result: Dict[str, Any]):
    if result.get("error_class"):
        status = 404 if str(result["error_class"]).endswith("not_found") else 400
        return JSONResponse(status_code=status, content=result)
    return result


async def _json(request: Request) -> Dict[str, Any]:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    return body if isinstance(body, dict) else {}


def setup_loop_card_routes() -> APIRouter:
    router = APIRouter(tags=["loop-cards"])

    def owner_of(request: Request) -> str:
        require_admin(request)
        return effective_user(request) or ""

    @router.get("/api/loop-cards")
    async def list_cards(request: Request, project: str = ""):
        return _reply(loop_card.run(owner_of(request), {"action": "list", "project": project or None}))

    @router.post("/api/loop-cards")
    async def create_card(request: Request):
        owner = owner_of(request)
        return _reply(await asyncio.to_thread(loop_card.run, owner, {**(await _json(request)), "action": "create"}))

    @router.get("/api/loop-cards/{card_id}")
    async def get_card(card_id: str, request: Request):
        return _reply(loop_card.run(owner_of(request), {"action": "get", "id": card_id}))

    @router.post("/api/loop-cards/{card_id}/check")
    async def check_card(card_id: str, request: Request):
        owner = owner_of(request)
        return _reply(await asyncio.to_thread(loop_card.run, owner, {**(await _json(request)), "action": "check", "id": card_id}))

    @router.delete("/api/loop-cards/{card_id}")
    async def delete_card(card_id: str, request: Request):
        return _reply(loop_card.run(owner_of(request), {"action": "delete", "id": card_id}))

    return router
