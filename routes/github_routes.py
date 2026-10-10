"""routes/github_routes.py -- API behind the `/github` composer command
(src/github_ops.py): labels, release notes, release check, and the approved
label sync.

    POST /api/github/command              {workspace, args, lang?}  -> {markdown, ...}
    POST /api/github/labels/sync/apply    {approval_id, lang?}      -> {markdown, ...}
    GET  /api/github/status?workspace=    -> gh/auth/remote diagnostics (no tokens)

`/command` is read-only. `labels sync <file>` returns the PLAN plus, only
when the repository policy allows remote writes (``push``), a single-use
``approval_id``; `/labels/sync/apply` is the human's yes -- it re-checks the
policy and that the plan is unchanged before `gh label create|edit` runs.

Admin only, like the staged review: the commands run `gh` as whichever
account the machine is signed in with. `workspace` must be an existing
absolute directory inside a git repository; ownership is the resolved
caller identity plus that check, logged for audit.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from core.middleware import require_admin
from src.auth_helpers import effective_user, require_user

logger = logging.getLogger(__name__)


def _error(status: int, detail: str, error_class: str = "args.bad") -> JSONResponse:
    return JSONResponse(status_code=status, content={"ok": False, "error": detail, "error_class": error_class,
                                                      "markdown": detail})


class CommandBody(BaseModel):
    workspace: str
    args: Optional[str] = ""
    lang: Optional[str] = "en"


class ApplyBody(BaseModel):
    approval_id: str
    lang: Optional[str] = None


def _repo_root(workspace: str) -> Optional[str]:
    from src import git_panel
    workspace = (workspace or "").strip()
    if not workspace or not os.path.isabs(workspace) or not os.path.isdir(workspace):
        return None
    return git_panel.repo_toplevel(workspace)


def setup_github_routes(session_manager: Any = None) -> APIRouter:
    router = APIRouter(prefix="/api/github", tags=["github"])

    @router.post("/command")
    async def post_command(request: Request, body: CommandBody,
                           _u: str = Depends(require_user), _a: None = Depends(require_admin)) -> Any:
        from src import github_ops
        owner = effective_user(request)
        root = _repo_root(body.workspace)
        if not root:
            return _error(400, "workspace must be an existing absolute directory inside a git repository", "git.not_a_repo")
        logger.info("[github] /github %r by %r in %r", (body.args or "")[:80], owner or "?", root)
        result = await asyncio.to_thread(github_ops.run_command, owner, root, body.args or "", body.lang or "en")
        return result

    @router.post("/labels/sync/apply")
    async def post_apply(request: Request, body: ApplyBody,
                         _u: str = Depends(require_user), _a: None = Depends(require_admin)) -> Any:
        from src import github_ops
        owner = effective_user(request)
        logger.info("[github] label sync approved by %r", owner or "?")
        result = await asyncio.to_thread(github_ops.apply_label_sync, owner, body.approval_id, body.lang or "")
        if not result.get("ok") and "markdown" not in result:
            result["markdown"] = result.get("error", "")
        return result

    @router.get("/status")
    async def get_status(workspace: str = "", _u: str = Depends(require_user), _a: None = Depends(require_admin)) -> Any:
        from src import git_github, github_ops
        info = await asyncio.to_thread(git_github.gh_accounts)
        out: dict = {"gh_available": info.get("available", False), "gh_version": info.get("version"),
                     "accounts": [a.get("login") for a in info.get("accounts", [])]}
        root = _repo_root(workspace) if workspace else None
        if root:
            ctx = await asyncio.to_thread(github_ops.repo_context, root)
            out["repo"] = {"root": root, "slug": ctx.get("slug"), "github": ctx.get("github"), "reason": ctx.get("reason")}
        return out

    return router
