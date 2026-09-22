"""routes/project_rules_routes.py — REST API for per-project rule files and
the bundled rule library (`src/project_rules.py`, lot C).

    GET  /api/rules/library              -> {"rules": [...], "count": n}
    GET  /api/rules?workspace=           -> discovered project rules, the
                                             languages detected, whether the
                                             folder is trusted, and the exact
                                             block() text an agent turn would
                                             inject
    POST /api/rules/install   {workspace, ids}   -> {"results": [...]}
    POST /api/rules/uninstall {workspace, ids}   -> {"results": [...]}

`workspace` goes through the same trust-on-first-use check
(`src.workspace_trust.instructions_trusted`) `src/agent_loop.py` applies
before injecting `project_instructions.block()` — an unapproved folder's own
rule files never reach this response's `block` field, only their names.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from src import project_rules

logger = logging.getLogger(__name__)


def _trusted_for(workspace: str) -> bool:
    try:
        from src import workspace_trust
        return bool(workspace_trust.instructions_trusted(workspace))
    except Exception as exc:  # noqa: BLE001 - no gate available -> do not block the response
        logger.debug("project_rules_routes: trust check unavailable: %s", exc)
        return True


class InstallRequest(BaseModel):
    workspace: str
    ids: List[str] = Field(default_factory=list)


class UninstallRequest(BaseModel):
    workspace: str
    ids: List[str] = Field(default_factory=list)


def setup_project_rules_routes() -> APIRouter:
    router = APIRouter(prefix="/api/rules", tags=["project-rules"])

    @router.get("/library")
    async def get_library() -> Dict[str, Any]:
        rows = project_rules.library()
        return {"rules": rows, "count": len(rows)}

    @router.get("")
    async def get_rules(workspace: str = Query(default="")) -> Dict[str, Any]:
        ws = (workspace or "").strip()
        if not ws:
            return {"workspace": "", "languages": [], "trusted": True,
                    "project_rules": [], "block": ""}
        root = os.path.realpath(os.path.expanduser(ws))
        if not os.path.isdir(root):
            raise HTTPException(status_code=400, detail="workspace is not a folder")
        trusted = _trusted_for(root)
        languages = project_rules.languages_for(root)
        return {
            "workspace": root,
            "languages": languages,
            "trusted": trusted,
            "project_rules": project_rules.project_rules(root),
            "block": project_rules.block(root, trusted=trusted, languages=languages),
        }

    @router.post("/install")
    async def install(body: InstallRequest) -> Dict[str, Any]:
        result = project_rules.install(body.workspace, body.ids)
        if "error" in result:
            raise HTTPException(status_code=400, detail=result["error"])
        return result

    @router.post("/uninstall")
    async def uninstall(body: UninstallRequest) -> Dict[str, Any]:
        result = project_rules.uninstall(body.workspace, body.ids)
        if "error" in result:
            raise HTTPException(status_code=400, detail=result["error"])
        return result

    return router
