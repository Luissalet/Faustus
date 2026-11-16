"""routes/web_guidance_routes.py - the vendored modern web guidance (src/web_guidance.py).

    GET /api/web-guidance/status                     what is vendored: version, guide count, categories
    GET /api/web-guidance/list?category=             every guide with its description
    GET /api/web-guidance/search?q=&limit=&category= ranked guides for a short capability query
    GET /api/web-guidance/guide/{id}?offset=&max_chars=   the guide text, in slices when it is long

Read-only; the guides are files shipped with the app.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException

from src import web_guidance


def setup_web_guidance_routes() -> APIRouter:
    router = APIRouter(prefix="/api/web-guidance", tags=["web-guidance"])

    @router.get("/status")
    def status():
        return web_guidance.status()

    @router.get("/list")
    def list_guides(category: Optional[str] = None):
        return web_guidance.list_guides(category)

    @router.get("/search")
    def search(q: str = "", limit: int = web_guidance.DEFAULT_LIMIT, category: Optional[str] = None):
        return web_guidance.search(q, limit, category)

    @router.get("/guide/{guide_id}")
    def guide(guide_id: str, offset: int = 0, max_chars: int = web_guidance.DEFAULT_MAX_CHARS):
        found = web_guidance.retrieve([guide_id], offset, max_chars)
        if not found["guides"]:
            raise HTTPException(status_code=404, detail={"error": "no such guide", **(found.get("missing") or [{}])[0]})
        return found["guides"][0]

    return router
