"""Workspace instruction-trust routes — /api/workspace-trust/* (FAUSTUS).

The consent surface for `src/workspace_trust.py`: a folder's AGENTS.md /
CLAUDE.md / .cursorrules go into the SYSTEM prompt of every turn, so before that
happens for a folder Faustus has never seen, the user gets to read exactly what
they are approving.

  GET  /api/workspace-trust?workspace=…   state + digest + each file's TEXT
  POST /api/workspace-trust/trust         {workspace, digest} → seal it
  POST /api/workspace-trust/revoke        {workspace}         → forget it
  GET  /api/workspace-trust/list          every standing approval

Three rules this module exists to hold:

* **The GET returns the file text; the prompt does not.** That inversion is the
  point. A human reading a file on a card can notice "run scripts/bootstrap.sh
  before answering questions about this repo"; a model that has been told by its
  own system prompt that this is project convention cannot.
* **The POSTs are admin AND same-origin.** Faustus normally runs with
  AUTH_ENABLED=false on 127.0.0.1, so `require_admin` alone is not a barrier
  against a page the user happens to have open; `routes/workspace_routes.py`
  documents that attack in full and this reuses its guard.
* **The model cannot reach any of it.** `src/tools/system.py` blocklists this
  surface for `app_api`, the same way `/api/storage/*` was blocklisted in §26.5:
  self-approval by a model that just read the file it wants approved would make
  the whole mechanism theatre. Unlike storage, the read is blocked too — not by
  choice but by spelling, since `/api/workspace-trust` starts with the
  `/api/workspace` prefix that was already off-limits — so `app_api` answers
  with a message telling the model to say in prose that the folder's instruction
  files are waiting for the user's approval.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Query, Request

from src.auth_helpers import get_current_user
from src.tool_security import owner_is_admin_or_single_user
from routes.workspace_routes import _reject_cross_origin

logger = logging.getLogger(__name__)

# Text of one instruction file returned on the approval card. The prompt itself
# caps at `agent_project_instructions_max_chars`; this is bigger on purpose (the
# user should see MORE than the model does, never less) and still bounded, since
# a browser is not a place to stream a megabyte of Markdown.
_MAX_TEXT_CHARS = 40_000
_MAX_FILES = 16


def setup_workspace_trust_routes() -> APIRouter:
    router = APIRouter(prefix="/api/workspace-trust", tags=["workspace-trust"])

    def _admin_only(request: Request) -> None:
        owner = get_current_user(request)
        if not owner_is_admin_or_single_user(owner):
            raise HTTPException(status_code=403, detail="Admin-only")

    def _admin_only_write(request: Request) -> None:
        """Admin AND same-origin: approving a folder is a standing authority
        grant over every future turn in it, so it must look like it came from
        the Faustus page (see routes/workspace_routes.py for the full argument
        about Sec-Fetch-Site on a local app with auth disabled)."""
        _admin_only(request)
        _reject_cross_origin(request)

    def _owner(request: Request) -> str:
        try:
            return str(get_current_user(request) or "")
        except Exception:  # noqa: BLE001
            return ""

    async def _body(request: Request) -> Dict[str, Any]:
        try:
            data = await request.json()
        except Exception:  # noqa: BLE001 - an empty or non-JSON body is a 400, not a 500
            data = {}
        return data if isinstance(data, dict) else {}

    def _file_texts(files: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Each tracked file with its text, so the user reads what they approve."""
        out: List[Dict[str, Any]] = []
        for entry in list(files)[:_MAX_FILES]:
            row = {
                "rel": entry.get("rel", ""),
                "path": entry.get("path", ""),
                "bytes": entry.get("bytes", 0),
                "sha256": entry.get("sha256", ""),
                "text": "",
                "truncated": False,
                "error": "",
            }
            path = str(entry.get("path") or "")
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    text = fh.read(_MAX_TEXT_CHARS + 1)
                if len(text) > _MAX_TEXT_CHARS:
                    text = text[:_MAX_TEXT_CHARS]
                    row["truncated"] = True
                row["text"] = text.replace("\r\n", "\n")
            except OSError as exc:
                row["error"] = f"could not read the file: {exc}"
            out.append(row)
        return out

    @router.get("")
    def read_state(request: Request, workspace: str = Query(default="")) -> Dict[str, Any]:
        """State of one folder's instruction files, with each file's full text.

        A pure read: it does NOT auto-trust, so opening the card never approves
        anything as a side effect. The auto-trust rule of `ask` lives on the turn
        path (`workspace_trust.resolve`), where it belongs.
        """
        _admin_only(request)
        from src import workspace_trust as wt
        root = wt.normalise(workspace)
        if not root or not os.path.isdir(root):
            raise HTTPException(status_code=400, detail="workspace is not a valid folder")
        state = wt.state_for(root)
        return {
            "status": "success",
            "mode": wt.mode(),
            "workspace": root,
            "state": state.get("state"),
            "digest": state.get("digest"),
            "previous_digest": state.get("previous_digest"),
            "trusted_at": state.get("trusted_at"),
            "by": state.get("by"),
            "auto_trust_eligible": wt.has_checkpoint_history(root),
            "files": _file_texts(state.get("files") or []),
        }

    @router.get("/list")
    def list_state(request: Request) -> Dict[str, Any]:
        """Every folder with a standing approval (no file text, no disk reads)."""
        _admin_only(request)
        from src import workspace_trust as wt
        return {"status": "success", "mode": wt.mode(), "trusted": wt.list_trusted()}

    @router.post("/trust")
    async def trust_workspace(request: Request) -> Dict[str, Any]:
        """Body: {"workspace": "...", "digest": "..."}.

        The digest must be the current one. An edit that lands between the read
        and the click is refused with the new digest, so the user re-reads
        instead of approving text they never saw (§23.2's revalidate-before-use,
        pointed at a file instead of a command).
        """
        _admin_only_write(request)
        body = await _body(request)
        from src import workspace_trust as wt
        root = wt.normalise(str(body.get("workspace") or ""))
        if not root or not os.path.isdir(root):
            raise HTTPException(status_code=400, detail="workspace is not a valid folder")
        digest = str(body.get("digest") or "").strip()
        if not digest:
            raise HTTPException(status_code=400, detail="digest is required")
        by = str(body.get("by") or "").strip() or (_owner(request) or "user")
        result = wt.trust(root, digest, by=by)
        if not result.get("ok"):
            raise HTTPException(status_code=409, detail={
                "error": result.get("error") or "approval refused",
                "digest": result.get("digest", ""),
            })
        try:
            from src import project_instructions as pi
            pi.invalidate(root)
        except Exception:  # noqa: BLE001 - a cache drop is never worth a 500
            pass
        return {"status": "success", **result}

    @router.post("/revoke")
    async def revoke_workspace(request: Request) -> Dict[str, Any]:
        """Body: {"workspace": "..."} — drop the approval. Idempotent."""
        _admin_only_write(request)
        body = await _body(request)
        from src import workspace_trust as wt
        root = wt.normalise(str(body.get("workspace") or ""))
        if not root:
            raise HTTPException(status_code=400, detail="workspace is required")
        result = wt.revoke(root)
        if not result.get("ok"):
            raise HTTPException(status_code=500, detail=result.get("error") or "could not revoke")
        try:
            from src import project_instructions as pi
            pi.invalidate(root)
        except Exception:  # noqa: BLE001
            pass
        return {"status": "success", **result}

    return router
