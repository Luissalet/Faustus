"""Operator catalogue and local source checkouts; never starts plugin code."""
from __future__ import annotations

import asyncio
from typing import Callable

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from core.middleware import require_admin
from src import plugin_marketplace as marketplace


class LinkSource(BaseModel):
    path: str = Field(min_length=1, max_length=4096)


async def _run(operation: Callable, *args):
    try:
        return await asyncio.to_thread(operation, *args)
    except marketplace.MarketplaceError as exc:
        code = str(exc.code)
        status = 404 if code in ("not_found", "unknown_plugin") else 409 if code in (
            "conflict", "destination_exists", "required_missing", "busy", "already_present", "required_unavailable",
        ) else 503 if code in ("git_unavailable", "git_timeout", "git_failed") else 400
        raise HTTPException(status_code=status, detail=str(exc.message)) from exc


def setup_plugin_marketplace_routes() -> APIRouter:
    router = APIRouter(tags=["plugin-marketplace"])

    @router.get("/api/plugin-marketplace")
    async def list_sources(request: Request):
        require_admin(request)
        return await _run(marketplace.list_marketplace)

    @router.post("/api/plugin-marketplace/{plugin_id}/install")
    async def install_source(plugin_id: str, request: Request):
        require_admin(request)
        return await _run(marketplace.install, plugin_id)

    @router.post("/api/plugin-marketplace/{plugin_id}/link")
    async def link_source(plugin_id: str, body: LinkSource, request: Request):
        require_admin(request)
        return await _run(marketplace.link, plugin_id, body.path)

    @router.post("/api/plugin-marketplace/{plugin_id}/unlink")
    async def unlink_source(plugin_id: str, request: Request):
        require_admin(request)
        return await _run(marketplace.unlink, plugin_id)

    return router
