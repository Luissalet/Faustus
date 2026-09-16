"""routes/persona_routes.py — `/api/personas/*` (R4, Reach wave).

Agent personas as Markdown (`src/personas/`): 16 builtins + the user's own
overrides under `DATA_DIR/personas/*.md`. Gated `require_user` throughout,
the same class `routes/board_routes.py` uses for a user's own data with no
external side effect — a persona is prose stored locally, nothing is sent
anywhere by writing one.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from src.auth_helpers import require_user
from src.personas import loader, registry

logger = logging.getLogger(__name__)


class PersonaWriteRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=200_000)


def _persona_error(exc: loader.PersonaError) -> None:
    raise HTTPException(status_code=400, detail=str(exc))


def setup_persona_routes() -> APIRouter:
    router = APIRouter(prefix="/api/personas", tags=["personas"])

    @router.get("")
    async def list_personas(request: Request):
        require_user(request)
        result = registry.load_all()
        return {
            "personas": [p.to_dict() for p in result.personas],
            "errors": result.errors,
        }

    @router.get("/{slug}")
    async def get_persona(slug: str, request: Request):
        require_user(request)
        persona = registry.get_persona(slug)
        if persona is None:
            raise HTTPException(status_code=404, detail=f"no persona named '{slug}'")
        return persona.to_dict()

    @router.get("/{slug}/render")
    async def render_persona(slug: str, request: Request):
        require_user(request)
        block = registry.render_system_block(slug)
        if not block:
            raise HTTPException(status_code=404, detail=f"no persona named '{slug}'")
        return PlainTextResponse(block)

    @router.put("/{slug}")
    async def put_persona(slug: str, body: PersonaWriteRequest, request: Request):
        require_user(request)
        try:
            persona = registry.save_user_persona(slug, body.content)
        except loader.PersonaError as exc:
            _persona_error(exc)
        return persona.to_dict()

    @router.delete("/{slug}")
    async def delete_persona(slug: str, request: Request):
        require_user(request)
        deleted = registry.delete_user_persona(slug)
        if not deleted:
            raise HTTPException(
                status_code=404,
                detail=f"no user override for '{slug}' to delete (a builtin persona cannot be deleted, "
                       f"only overridden)",
            )
        return {"deleted": True, "slug": slug}

    return router
