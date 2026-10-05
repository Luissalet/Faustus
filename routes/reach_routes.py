"""Reach (R1) — /api/reach/*. Thin HTTP wrapper over `src/reach/router.py`
and `src/reach/doctor.py`. Admin-gated the same way `routes/external_runtimes_routes.py`
gates its config surface: this is a channel with per-user credentials and
outbound network calls, not a plain read for every signed-in user.

Error convention: flat `{"error_class": ..., "detail": ...}`
(`routes/git_routes.py::_error`).
"""
from __future__ import annotations

import logging
import asyncio
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from core.middleware import require_admin
from src.reach.extract import ExtractRequest
from src.reach.batch import BatchReadRequest
from src.reach.browser import BrowserRequest
from src.reach.crawl import CrawlRequest
from src.reach.recipes import RecipeRequest
from src.auth_helpers import effective_user

logger = logging.getLogger(__name__)


def _error(status: int, error_class: str, detail: Optional[str] = None, **extra: Any) -> JSONResponse:
    body: Dict[str, Any] = {"error_class": error_class}
    if detail is not None:
        body["detail"] = detail
    body.update(extra)
    return JSONResponse(status_code=status, content=body)


class ReadBody(BaseModel):
    url: str
    channel: Optional[str] = None


class SearchBody(BaseModel):
    query: str
    channels: Optional[List[str]] = None
    limit: int = 10


def setup_reach_routes() -> APIRouter:
    router = APIRouter(prefix="/api/reach", tags=["reach"])

    @router.post("/crawl")
    async def post_crawl(request: Request, body: CrawlRequest) -> Any:
        require_admin(request)
        from src.reach.crawl import run_crawl
        try:
            return await asyncio.to_thread(run_crawl, body, owner=effective_user(request))
        except Exception as exc:
            return _error(400, "crawl_failed", str(exc))

    @router.post("/browser")
    async def post_browser(request: Request, body: BrowserRequest) -> Any:
        require_admin(request)
        from src.reach.browser import run_browser
        try:
            return await run_browser(body, owner=effective_user(request))
        except Exception as exc:
            return _error(400, "browser_failed", str(exc))

    @router.post("/extract")
    async def post_extract(request: Request, body: ExtractRequest) -> Any:
        require_admin(request)
        from src.reach.extract import extract
        try:
            return await extract(body, owner=effective_user(request))
        except Exception as exc:
            return _error(400, "extraction_failed", str(exc))

    @router.post("/read-many")
    async def post_read_many(request: Request, body: BatchReadRequest) -> Any:
        require_admin(request)
        from src.reach.batch import read_many
        try:
            return await read_many(body, ctx={"owner": effective_user(request)})
        except ValueError as exc:
            return _error(400, "invalid_request", str(exc))

    @router.post("/recipe")
    async def post_recipe(request: Request, body: RecipeRequest) -> Any:
        require_admin(request)
        from src.reach.recipes import run_recipe
        try:
            return await run_recipe(body, owner=effective_user(request))
        except Exception as exc:
            return _error(400, 'recipe_failed', str(exc))

    @router.get("/doctor")
    async def get_doctor(request: Request, live: int = 0) -> Dict[str, Any]:
        require_admin(request)
        from src.reach.doctor import doctor as run_doctor
        return await run_doctor(live=bool(live))

    @router.post("/read")
    async def post_read(request: Request, body: ReadBody) -> Any:
        require_admin(request)
        if not body.url or not body.url.strip():
            return _error(400, "invalid_request", "url is required")
        from src.reach import router as reach_router
        try:
            result = await reach_router.read(body.url.strip(), channel=body.channel, ctx={"owner": effective_user(request)})
        except Exception as exc:  # noqa: BLE001
            logger.warning("reach.read failed for %r: %s", body.url, exc)
            return _error(502, "reach_read_failed", str(exc))
        return result.to_dict()

    @router.post("/search")
    async def post_search(request: Request, body: SearchBody) -> Any:
        require_admin(request)
        if not body.query or not body.query.strip():
            return _error(400, "invalid_request", "query is required")
        from src.reach import router as reach_router
        try:
            by_channel = await reach_router.search(body.query.strip(), channels=body.channels, limit=body.limit)
        except Exception as exc:  # noqa: BLE001
            logger.warning("reach.search failed for %r: %s", body.query, exc)
            return _error(502, "reach_search_failed", str(exc))
        return {name: [r.to_dict() for r in results] for name, results in by_channel.items()}

    return router
