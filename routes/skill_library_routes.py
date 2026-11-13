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
import asyncio
from urllib.parse import urlencode, quote
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

    @router.get("/agent-flows")
    async def agent_flows(request: Request, app: str = "", q: str = "") -> Dict[str, Any]:
        _owner(request)
        from src import hoard_hub
        status, data, where = await asyncio.to_thread(hoard_hub._call, "GET", "/api/skills/flows?" + urlencode({"app": app, "q": q}), timeout=30.0)
        if status != 200 or not isinstance(data, dict):
            return {"ok": False, "error": "The Hub flow catalogue is unavailable. Check the Hub connection and retry."}
        for row in data.get("apps", []):
            row["shelf_url"] = where["url"] + row["shelf_url"]
            for flow in row.get("flows", []):
                flow["skill_url"] = "/api/skills/library/agent-flow-skill?" + urlencode({"app": row["id"], "slug": flow["skill"]})
        return data

    @router.get("/agent-flow-skill")
    async def agent_flow_skill(request: Request, app: str, slug: str) -> Dict[str, Any]:
        _owner(request)
        from src import hoard_hub
        status, data, _ = await asyncio.to_thread(hoard_hub._call, "GET", "/api/skills/app/" + quote(app, safe="") + "/read/" + quote(slug, safe=""))
        if status != 200 or not isinstance(data, dict):
            return {"ok": False, "error": "The skill could not be read. Check the Hub connection and retry."}
        return data

    @router.get("")
    async def list_library(request: Request) -> Dict[str, Any]:
        owner = _owner(request)
        rows = skill_library.list_library(owner=owner)
        try:
            from src import connector_skills
            shelf = connector_skills.served_by()
            if shelf:
                rows = [({**r, "served_by": shelf[r["slug"]]} if r.get("slug") in shelf else r) for r in rows]
        except Exception:
            pass
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
