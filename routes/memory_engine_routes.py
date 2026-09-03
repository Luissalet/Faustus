"""Learned-memory API — /api/memory-engine/* (FAUSTUS).

The Brain page's "Learned rules" section talks to these six endpoints. They
are the human end of the loop the agent runs on its own: read what was
learned, add a rule by hand (which lands with the highest trust class,
``human_explicit``), vote a rule up or down, delete one, run the Curator, and
see the EXACT block the model would be given for a query.

Admin-only, like the rest of the brain: the store holds standing instructions
the agent will follow, so writing to it is an authority change.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from core.middleware import require_admin
from src.auth_helpers import effective_user

logger = logging.getLogger(__name__)


class ItemCreate(BaseModel):
    text: str
    level: Optional[str] = None
    category: Optional[str] = None
    project: Optional[str] = None


class FeedbackBody(BaseModel):
    kind: str
    reason: Optional[str] = None


class CurateBody(BaseModel):
    project: Optional[str] = None


def _owner(request: Request) -> str:
    try:
        return str(effective_user(request) or "")
    except Exception:  # noqa: BLE001 - attribution must not 500 the route
        return ""


def setup_memory_engine_routes() -> APIRouter:
    router = APIRouter(prefix="/api/memory-engine", tags=["memory-engine"])

    @router.get("/items")
    async def list_items(
        request: Request,
        project: Optional[str] = None,
        status: Optional[str] = None,
        level: Optional[str] = None,
        limit: int = 200,
        _admin: None = Depends(require_admin),
    ) -> Dict[str, Any]:
        """Items with their COMPUTED effective_score / harmful_ratio."""
        from src import memory_engine as engine
        owner = _owner(request)
        try:
            items = engine.list_items(owner=owner, project=project, status=status,
                                      level=level, limit=limit)
        except engine.MemoryEngineError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        now = engine._utcnow()
        return {
            "status": "success",
            "items": [engine.public_item(item, now) for item in items],
            "stats": engine.stats(owner, project),
            "levels": list(engine.LEVELS),
            "trust_classes": dict(engine.TRUST_CLASSES),
        }

    @router.post("/items")
    async def create_item(request: Request, body: ItemCreate,
                          _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        """A human wrote this down, so it lands as ``human_explicit`` (0.85) —
        the highest trust class the store has."""
        from src import memory_engine as engine
        try:
            item = engine.add_item(
                body.text,
                owner=_owner(request),
                project=str(body.project or ""),
                level=body.level or "procedural",
                category=body.category or "",
                trust_class="human_explicit",
                evidence=[{"kind": "chat", "excerpt": "added by the owner in the Brain page"}],
            )
        except engine.MemoryEngineError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"status": "success", "item": engine.public_item(item)}

    @router.post("/items/{item_id}/feedback")
    async def item_feedback(item_id: str, body: FeedbackBody,
                            _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        from src import memory_engine as engine
        try:
            item = engine.add_feedback(item_id, body.kind, reason=body.reason or "",
                                       ref=f"human:{item_id}")
        except engine.MemoryEngineError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if not item:
            raise HTTPException(status_code=404, detail="no such memory item")
        return {"status": "success", "item": engine.public_item(item)}

    @router.delete("/items/{item_id}")
    async def remove_item(item_id: str,
                          _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        from src import memory_engine as engine
        if not engine.delete_item(item_id):
            raise HTTPException(status_code=404, detail="no such memory item")
        return {"status": "success", "deleted": True, "id": item_id}

    @router.post("/curate")
    async def run_curator(request: Request, body: Optional[CurateBody] = None,
                          _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        """Layer 2, on demand. Deterministic — no model is called."""
        from src import memory_engine as engine
        from src import memory_curator
        project = (body.project if body else None)
        report = memory_curator.safe_curate(owner=_owner(request), project=project)
        return {"status": "success", "report": report,
                "stats": engine.stats(_owner(request), project)}

    @router.get("/pack")
    async def preview_pack(
        request: Request,
        project: Optional[str] = None,
        query: str = "",
        _admin: None = Depends(require_admin),
    ) -> Dict[str, Any]:
        """The exact block the model would see — nothing regenerated, the same
        function the prompt builder calls."""
        from src import memory_engine as engine
        try:
            detail = engine.pack_detail(_owner(request), project, query,
                                        engine.injection_budget())
        except Exception as exc:  # noqa: BLE001 - mirrors pack()'s own posture
            logger.debug("memory engine: pack preview failed: %s", exc)
            detail = {"text": "", "ids": [], "degraded": False}
        return {
            "status": "success",
            "pack": detail.get("text") or "",
            "ids": detail.get("ids") or [],
            "degraded": bool(detail.get("degraded")),
            "chars": len(detail.get("text") or ""),
            "budget": engine.injection_budget(),
            "enabled": engine.injection_enabled(),
        }

    return router
