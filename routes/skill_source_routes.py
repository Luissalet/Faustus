# routes/skill_source_routes.py
"""REST API for A25's git-backed skill sources (`src/skill_sources.py`).

Admin-only: installing/updating a skill from an arbitrary git URL runs `git
clone`/`checkout` against wherever that URL points and (for `update`) can
run a caller's fixture callback — the same trust level as the container
execution routes, not the read-mostly `/api/skills` surface.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from core.middleware import require_admin
from src import skill_sources

logger = logging.getLogger(__name__)


class SkillSourceInstallRequest(BaseModel):
    skill_dir: str = Field(..., min_length=1, max_length=1000)
    source_url: str = Field(..., min_length=1, max_length=2000)
    ref: str = Field("HEAD", min_length=1, max_length=200)
    skill_key: Optional[str] = Field(None, max_length=1000)


class SkillSourceUpdateRequest(BaseModel):
    verify: bool = True


def _key_or_404(skill_key: str) -> dict:
    src = skill_sources.get_source(skill_key)
    if src is None:
        raise HTTPException(404, "No git source recorded for this skill")
    return src


def setup_skill_source_routes() -> APIRouter:
    router = APIRouter(prefix="/api/skill-sources", tags=["skill-sources"])

    @router.post("")
    async def install_skill_source(body: SkillSourceInstallRequest, request: Request):
        require_admin(request)
        import asyncio
        try:
            record = await asyncio.to_thread(
                skill_sources.install, body.skill_dir, body.source_url, body.ref,
                skill_key=body.skill_key,
            )
        except skill_sources.SkillSourceError as e:
            raise HTTPException(400, str(e))
        return {"source": record}

    # Order matters: FastAPI/Starlette matches routes in declaration order
    # and `{skill_key:path}` is greedy, so the more specific "/check-updates"
    # etc. suffixed routes MUST be declared before the bare
    # "/{skill_key:path}" GET below, or it swallows "<key>/check-updates" as
    # a literal skill_key and every suffixed route 404s.
    @router.get("/{skill_key:path}/check-updates")
    async def check_skill_updates(skill_key: str, request: Request):
        require_admin(request)
        import asyncio
        try:
            status = await asyncio.to_thread(skill_sources.check_updates, skill_key)
        except skill_sources.SkillSourceError as e:
            raise HTTPException(404, str(e))
        return status

    @router.post("/{skill_key:path}/update")
    async def update_skill_source(skill_key: str, body: SkillSourceUpdateRequest, request: Request):
        require_admin(request)
        import asyncio
        try:
            result = await asyncio.to_thread(
                skill_sources.update, skill_key, verify=body.verify,
            )
        except skill_sources.SkillSourceError as e:
            raise HTTPException(404, str(e))
        return result

    @router.post("/{skill_key:path}/rollback")
    async def rollback_skill_source(skill_key: str, request: Request):
        require_admin(request)
        import asyncio
        try:
            record = await asyncio.to_thread(skill_sources.rollback, skill_key)
        except skill_sources.SkillSourceError as e:
            raise HTTPException(400, str(e))
        return {"source": record}

    @router.get("/{skill_key:path}")
    async def get_skill_source(skill_key: str, request: Request):
        require_admin(request)
        return {"source": _key_or_404(skill_key)}

    return router
