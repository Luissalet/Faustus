"""routes/design_hook_routes.py - the design check of UI edits (src/design_hook).

    GET  /api/design-hook/status?session_id=      can the design rules be asked now, and the chat's last closing pass
    POST /api/design-hook/start                   turn the workshop's design module on (explicit; nothing else starts it)
    POST /api/design-hook/run  {project_id|workspace, paths, level}   run the rules on demand

`level` is `static` (the unambiguous Floor rules, read from the file alone, no browser) or `full` (every static rule
plus a render and the browser rules of each page). The project's exceptions apply to a run made with `project_id`.
The workshop is only asked: when it is stopped, or its design module is off, the answer says so and nothing is started. Whether the check runs by
itself after the agent's edits is the project's `design_hook` setting (PATCH /api/projects/{id}).
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from core.middleware import require_admin
from src import design_hook
from src.auth_helpers import get_current_user
from src.owner_identity import is_request_sentinel_owner


class RunRequest(BaseModel):
    project_id: Optional[str] = Field(None, max_length=200)
    workspace: Optional[str] = Field(None, max_length=4096)
    paths: List[str] = Field(default_factory=list, max_length=40)
    level: str = Field("static", pattern="^(static|full)$")


def setup_design_hook_routes() -> APIRouter:
    router = APIRouter(prefix="/api/design-hook", tags=["design-hook"], dependencies=[Depends(require_admin)])

    @router.get("/status")
    async def get_status(session_id: str = "") -> Dict[str, Any]:
        return await asyncio.to_thread(design_hook.status, session_id)

    @router.post("/start")
    async def start() -> Dict[str, Any]:
        return await asyncio.to_thread(design_hook.start_module)

    @router.post("/run")
    async def run(request: Request, body: RunRequest) -> Dict[str, Any]:
        user = get_current_user(request)
        owner = None if is_request_sentinel_owner(user) else user
        workspace, waivers = (body.workspace or "").strip(), []
        if body.project_id:
            from services import projects
            project = projects.get_store().get(body.project_id, owner)
            if not project:
                raise HTTPException(404, "no such project")
            workspace = workspace or str(project.get("workspace") or "")
            waivers = list(project.get("design_waivers") or [])
        if not workspace or not os.path.isdir(workspace):
            raise HTTPException(400, "give a project_id with a folder, or a workspace that exists")
        if not body.paths:
            raise HTTPException(400, "give the UI files to check in paths")
        return await asyncio.to_thread(design_hook.check, workspace, body.paths, level=body.level, waivers=waivers)

    return router
