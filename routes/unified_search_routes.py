"""Unified search — ``GET /api/search/all``.

Thin HTTP wrapper over :mod:`src.unified_search`: resolve the caller the
same way ``routes/brain_routes.py`` / ``routes/chat_routes.py`` do for
owner scoping, then run the (synchronous, thread-pooled) search off the
event loop.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query, Request

from src import unified_search
from src.auth_helpers import effective_user


def setup_unified_search_routes() -> APIRouter:
    router = APIRouter(tags=["search"])

    @router.get("/api/search/all")
    async def search_all(
        request: Request,
        q: str = Query("", min_length=0),
        types: str = Query(""),
        limit: int = Query(8, ge=1, le=25),
    ) -> Dict[str, Any]:
        import asyncio

        owner = effective_user(request)
        wanted: Optional[List[str]] = None
        if types.strip():
            wanted = [t.strip() for t in types.split(",") if t.strip()]
        return await asyncio.to_thread(unified_search.search, owner, q, wanted, limit)

    return router


__all__ = ["setup_unified_search_routes"]
