"""routes/harness_proposals_routes.py - REST API for harness refinement proposals.

    GET  /api/harness/proposals?status=&axis=&session_id=&limit=   list (newest first)
    GET  /api/harness/proposals/status                             setting, queue, counts
    GET  /api/harness/proposals/log?limit=&proposal_id=            trigger -> result log
    GET  /api/harness/proposals/{id}                               one record, with exact before/after
    POST /api/harness/proposals/propose   {session_id, force}      look at a session now
    POST /api/harness/proposals/{id}/approve                       apply it (a person only)
    POST /api/harness/proposals/{id}/reject  {reason}              discard it (a person only)
    POST /api/harness/proposals/{id}/undo                          restore the exact previous content

Approve / reject / undo sit behind ``require_human``: the in-process tool token
that lets the agent call admin routes is refused there, so the model cannot
approve its own proposal by calling the endpoint the card calls. ``propose`` is
admin-gated and works whether or not ``harness_refinement_enabled`` is on: the
setting only controls the automatic run after each task.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from core.middleware import require_admin, require_human
from src.auth_helpers import get_current_user
from src.owner_identity import is_request_sentinel_owner
from src.harness_refinement import proposer, runner, store, targets
from src.harness_refinement.store import HarnessError

logger = logging.getLogger(__name__)


class ProposeRequest(BaseModel):
    session_id: str = Field(..., min_length=1, max_length=200)
    force: bool = False


class RejectRequest(BaseModel):
    reason: str = Field("", max_length=500)


def public(record: Dict[str, Any], *, full: bool = False, check_stale: bool = False) -> Dict[str, Any]:
    out = {k: v for k, v in record.items() if k not in ("before", "after", "applied_content", "meta")}
    out["has_delegate"] = bool((record.get("meta") or {}).get("delegate"))
    if full:
        out["before"] = record.get("before")
        out["after"] = record.get("after")
        out["applied_content"] = record.get("applied_content")
    if check_stale and record.get("status") == "pending":
        try:
            current = targets.read_current(record["axis"], record["target"], record.get("owner") or "")
            out["stale"] = not targets.same_content(current, record.get("before"))
        except HarnessError:
            out["stale"] = True
    return out


def _raise(exc: HarnessError) -> None:
    raise HTTPException(status_code=exc.status_code, detail=exc.to_dict())


_RESULT_STATUS = {
    "session_not_found": 404, "no_endpoint": 409, "model_error": 502, "unparseable": 502,
}


def setup_harness_proposals_routes(memory_vector: Any = None) -> APIRouter:
    targets.configure(memory_vector=memory_vector)
    router = APIRouter(prefix="/api/harness/proposals", tags=["harness-proposals"])

    def _owner(request: Request) -> Optional[str]:
        user = get_current_user(request)
        # The agent's loopback token is not a data owner: it sees what the
        # (single) local user sees, exactly like the other admin routes.
        return None if is_request_sentinel_owner(user) else user

    def _visible(record: Optional[Dict[str, Any]], user: Optional[str]) -> bool:
        return bool(record) and (not user or record.get("owner") in ("", None, user))

    @router.get("")
    async def list_proposals(request: Request, status: Optional[str] = None, axis: Optional[str] = None,
                             session_id: Optional[str] = None, limit: int = Query(100, ge=1, le=500)) -> Dict[str, Any]:
        user = _owner(request)
        await asyncio.to_thread(store.sync_delegates)
        rows = await asyncio.to_thread(
            lambda: [public(r, check_stale=True) for r in store.list_proposals(
                status=status, axis=axis, session_id=session_id, owner=user, limit=limit)])
        return {"proposals": rows, "enabled": runner.enabled()}

    @router.get("/status")
    async def get_status(request: Request) -> Dict[str, Any]:
        user = _owner(request)
        return {**runner.status(), "counts": store.counts(owner=user), "axes": list(store.AXES)}

    @router.get("/log")
    async def get_log(request: Request, limit: int = Query(100, ge=1, le=500),
                      proposal_id: str = "") -> Dict[str, Any]:
        return {"log": store.read_log(limit=limit, proposal_id=proposal_id, owner=_owner(request))}

    @router.get("/{proposal_id}")
    async def get_one(request: Request, proposal_id: str) -> Dict[str, Any]:
        await asyncio.to_thread(store.sync_delegates)
        record = store.get(proposal_id)
        if not _visible(record, _owner(request)):
            raise HTTPException(404, detail={"error_class": "harness.not_found", "message": "no such proposal"})
        return {"proposal": public(record, full=True, check_stale=True),
                "log": store.read_log(limit=50, proposal_id=proposal_id)}

    @router.post("/propose")
    async def propose(request: Request, body: ProposeRequest, _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        user = _owner(request)
        result = await proposer.propose_for_session(
            body.session_id, owner=user or "", trigger="manual", user_initiated=True, force=body.force)
        if result.get("proposal"):
            result["proposal"] = public(result["proposal"], check_stale=True)
        status = result.get("status")
        reason = str(result.get("reason") or "")
        if status == "error":
            code = _RESULT_STATUS.get(reason, 422 if reason.startswith("harness.") else 400)
            raise HTTPException(status_code=code, detail={"error_class": reason if "." in reason else f"harness.{reason}",
                                                          "message": result.get("message") or reason})
        return result

    @router.post("/{proposal_id}/approve")
    async def approve(request: Request, proposal_id: str, _human: None = Depends(require_human)) -> Dict[str, Any]:
        user = _owner(request)
        if not _visible(store.get(proposal_id), user):
            raise HTTPException(404, detail={"error_class": "harness.not_found", "message": "no such proposal"})
        try:
            record = await asyncio.to_thread(store.approve, proposal_id, by=user or "local")
        except HarnessError as exc:
            _raise(exc)
        return {"proposal": public(record, full=True)}

    @router.post("/{proposal_id}/reject")
    async def reject(request: Request, proposal_id: str, body: Optional[RejectRequest] = None,
                     _human: None = Depends(require_human)) -> Dict[str, Any]:
        user = _owner(request)
        if not _visible(store.get(proposal_id), user):
            raise HTTPException(404, detail={"error_class": "harness.not_found", "message": "no such proposal"})
        try:
            record = await asyncio.to_thread(store.reject, proposal_id, by=user or "local",
                                             reason=(body.reason if body else ""))
        except HarnessError as exc:
            _raise(exc)
        return {"proposal": public(record)}

    @router.post("/{proposal_id}/undo")
    async def undo(request: Request, proposal_id: str, _human: None = Depends(require_human)) -> Dict[str, Any]:
        user = _owner(request)
        if not _visible(store.get(proposal_id), user):
            raise HTTPException(404, detail={"error_class": "harness.not_found", "message": "no such proposal"})
        try:
            record = await asyncio.to_thread(store.undo, proposal_id, by=user or "local")
        except HarnessError as exc:
            _raise(exc)
        return {"proposal": public(record, full=True), "undone": proposal_id}

    return router
