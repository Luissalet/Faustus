"""Farm state API - GET /api/farm/state (src/farm_state.py).

One document for "what is running right now": chat turns, the sub-agents under
them, dispatch jobs and their workers, night shifts, workflow runs, scheduled
tasks in progress, and the period budget. Built at most once a second and
served with an ETag, so a watcher polling every few seconds costs a 304.

Read-only. Reading needs a signed-in user, or an API token with the
`farm:read` scope (or `sessions`, which already opens the same chats); the
token's owner decides what is listed (core/authz.py).
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request, Response

from src.auth_helpers import _auth_disabled, effective_user


def setup_farm_routes() -> APIRouter:
    router = APIRouter(prefix="/api/farm", tags=["farm"])

    @router.get("/state")
    async def get_farm_state(request: Request, response: Response) -> Any:
        # `effective_user` (not `require_user`): a token with the right scope has
        # already been admitted by core/authz.py and resolves to its owner.
        owner = effective_user(request)
        if not owner and not _auth_disabled():
            raise HTTPException(401, "Authentication required")
        from src import farm_state
        body, etag = await asyncio.to_thread(farm_state.cached, owner or "")
        headers = {"ETag": etag, "Cache-Control": "no-cache"}
        if farm_state.etag_matches(request.headers.get("if-none-match"), etag):
            return Response(status_code=304, headers=headers)
        for key, value in headers.items():
            response.headers[key] = value
        out: Dict[str, Any] = body
        return out

    return router