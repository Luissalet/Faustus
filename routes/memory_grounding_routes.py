"""Grounding lint API — /api/memory-engine/grounding (FAUSTUS).

``src/memory_grounding.py`` checks that the concrete, checkable claims
inside a memory item (numbers, dates, quotes, names, URLs...) actually show
up in the evidence that item cites. This route exposes that check as a
read-only report for one owner's store — the Memory screen's "Grounding"
panel next to Conflicts.

Same auth pattern as ``routes/memory_engine_routes.py``'s ``/conflicts``:
admin-only, owner-scoped from the request's ``effective_user``.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Request

from core.middleware import require_admin
from src.auth_helpers import effective_user

logger = logging.getLogger(__name__)


def _owner(request: Request) -> str:
    try:
        return str(effective_user(request) or "")
    except Exception:  # noqa: BLE001 - attribution must not 500 the route
        return ""


def setup_memory_grounding_routes() -> APIRouter:
    router = APIRouter(prefix="/api/memory-engine", tags=["memory-grounding"])

    @router.get("/grounding")
    async def get_grounding(request: Request, limit: int = 200,
                            _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        """Flagged items (missing specifics, worst first) for this owner,
        plus how many items had no evidence to check at all."""
        from src import memory_grounding

        owner = _owner(request)
        report = memory_grounding.lint(owner=owner, limit=limit)
        return {"status": "success", **report}

    return router


__all__ = ["setup_memory_grounding_routes"]
