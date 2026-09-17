# routes/home_cards_routes.py
"""`/api/home/cards` — the automations pinned to Home and what they show.

Pinning is per owner (sidecar, src/home_cards.py); the card body is the
task's latest successful run. "Run now" is the existing
`POST /api/tasks/{id}/run`, so nothing about execution is duplicated here.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from src.auth_helpers import get_current_user
from src import home_cards


class PinBody(BaseModel):
    task_id: str
    position: Optional[int] = None


class OrderBody(BaseModel):
    order: List[str]


def _owner(request: Request) -> Optional[str]:
    return get_current_user(request) or None


def setup_home_cards_routes() -> APIRouter:
    router = APIRouter(prefix="/api/home/cards", tags=["home"])

    @router.get("")
    async def list_cards(request: Request) -> Dict[str, Any]:
        owner = _owner(request)
        return {"cards": home_cards.cards(owner)}

    @router.post("")
    async def pin_card(body: PinBody, request: Request) -> Dict[str, Any]:
        owner = _owner(request)
        from core.database import ScheduledTask, SessionLocal
        db = SessionLocal()
        try:
            task = db.query(ScheduledTask).filter(ScheduledTask.id == body.task_id).first()
            if not task:
                raise HTTPException(404, "Task not found")
            if owner and task.owner and task.owner != owner:
                raise HTTPException(403, "Access denied")
        finally:
            db.close()
        return {"cards": home_cards.pin(owner, body.task_id, position=body.position)}

    @router.delete("/{task_id}")
    async def unpin_card(task_id: str, request: Request) -> Dict[str, Any]:
        owner = _owner(request)
        return {"cards": home_cards.unpin(owner, task_id)}

    @router.put("/order")
    async def order_cards(body: OrderBody, request: Request) -> Dict[str, Any]:
        owner = _owner(request)
        return {"cards": home_cards.reorder(owner, body.order)}

    return router
