"""routes/skill_library_routes.py — REST API for the bundled skill library
(`src/skill_library.py`, lot C).

    GET  /api/skills/library                  -> {"skills": [...], "count": n}
    POST /api/skills/library/install           {slugs, replace?} -> {"results": [...]}
    POST /api/skills/library/uninstall          {slugs} -> {"results": [...]}

Owner-scoped exactly like `routes/skills_routes.py`: the caller only ever
sees and installs into their own skill store.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from src import skill_library
from src.auth_helpers import get_current_user

logger = logging.getLogger(__name__)


class InstallRequest(BaseModel):
    slugs: List[str] = Field(default_factory=list)
    replace: bool = False


class UninstallRequest(BaseModel):
    slugs: List[str] = Field(default_factory=list)


def setup_skill_library_routes() -> APIRouter:
    router = APIRouter(prefix="/api/skills/library", tags=["skill-library"])

    def _owner(request: Request):
        return get_current_user(request)

    @router.get("")
    async def list_library(request: Request) -> Dict[str, Any]:
        owner = _owner(request)
        rows = skill_library.list_library(owner=owner)
        return {"skills": rows, "count": len(rows)}

    @router.post("/install")
    async def install(body: InstallRequest, request: Request) -> Dict[str, Any]:
        owner = _owner(request)
        return skill_library.install(owner, body.slugs, replace=body.replace)

    @router.post("/uninstall")
    async def uninstall(body: UninstallRequest, request: Request) -> Dict[str, Any]:
        owner = _owner(request)
        return skill_library.uninstall(owner, body.slugs)

    return router
