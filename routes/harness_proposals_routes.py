"""routes/harness_proposals_routes.py - REST API for harness refinement proposals.

    GET  /api/harness/proposals?status=&axis=&session_id=&limit=   list (newest first)
    GET  /api/harness/proposals/status                             setting, queue, counts
    GET  /api/harness/proposals/log?limit=&proposal_id=            trigger -> result log
    GET  /api/harness/proposals/{id}                               one record, with exact before/after
    POST /api/harness/proposals/propose   {session_id, force}      look at a session now
    POST /api/harness/proposals/{id}/approve                       apply it (a person only)
    POST /api/harness/proposals/{id}/reject  {reason}              discard it (a person only)
    POST /api/harness/proposals/{id}/undo                          restore the exact previous content

Probed rounds (src/harness_refinement/probe.py): the task is repeated read-only in the background
with the harness as it is and with each candidate edit, and the edit that measurably helps is kept:

    GET  /api/harness/proposals/probes?session_id=&status=&limit=  list of rounds (light form)
    GET  /api/harness/proposals/probes/{id}                        one round: repeats, citations, selection
    POST /api/harness/proposals/probes   {session_id, force}       start a round now; returns at once
    GET  /api/harness/proposals/lessons?project_id=                domains with an EXPERIENCE.md, or its entries

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
from src.harness_refinement import experience, probe, probe_store, proposer, runner, store, targets
from src.harness_refinement.store import HarnessError

logger = logging.getLogger(__name__)


class ProposeRequest(BaseModel):
    session_id: str = Field(..., min_length=1, max_length=200)
    force: bool = False


class ProbeRequest(BaseModel):
    session_id: str = Field(..., min_length=1, max_length=200)
    force: bool = False


#: Rounds started from the API: kept so the event loop does not drop a task nobody awaits.
_BACKGROUND: set = set()


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

    @router.get("/probes")
    async def list_probes(request: Request, session_id: Optional[str] = None, status: Optional[str] = None,
                          limit: int = Query(30, ge=1, le=200)) -> Dict[str, Any]:
        rows = await asyncio.to_thread(
            lambda: [probe_store.summary(r) for r in probe_store.list_probes(
                owner=_owner(request), session_id=session_id, status=status, limit=limit)])
        return {"probes": rows, "probe": probe.probe_enabled(), "auto_apply": probe.auto_apply_enabled(),
                "k": probe.probe_k()}

    @router.get("/probes/{probe_id}")
    async def get_probe(request: Request, probe_id: str) -> Dict[str, Any]:
        await asyncio.to_thread(probe_store.reconcile)
        record = probe_store.get(probe_id)
        user = _owner(request)
        if not record or (user and record.get("owner") not in ("", None, user)):
            raise HTTPException(404, detail={"error_class": "harness.not_found", "message": "no such probe"})
        proposals = [public(r) for r in store.list_proposals(owner=user, limit=500) if r.get("probe_id") == probe_id]
        return {"probe": record, "proposals": proposals}

    @router.post("/probes")
    async def start_probe(request: Request, body: ProbeRequest, _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        """Start a probed round now. It runs for minutes, so this answers at once with the probe id."""
        user = _owner(request)
        if body.session_id in proposer._INFLIGHT:
            raise HTTPException(409, detail={"error_class": "harness.busy", "message": "a round is already running for this chat"})
        probe_id = probe_store.new_id()
        task = asyncio.create_task(probe.probe_for_session(
            body.session_id, owner=user or "", trigger="manual", user_initiated=True, force=body.force,
            probe_id=probe_id))
        _BACKGROUND.add(task)
        task.add_done_callback(_BACKGROUND.discard)
        await asyncio.sleep(0.2)                       # a round that cannot start answers its reason right away
        if task.done():
            result = task.result()
            if result.get("status") in ("skipped", "error", "none") and not probe_store.get(probe_id):
                reason = str(result.get("reason") or "")
                code = _RESULT_STATUS.get(reason, 409 if result.get("status") == "skipped" else 422)
                raise HTTPException(status_code=code, detail={"error_class": reason if "." in reason else f"harness.{reason}",
                                                              "message": result.get("message") or reason})
            return {"probe_id": result.get("probe_id") or probe_id, "status": result.get("status")}
        return {"probe_id": probe_id, "status": "running"}

    @router.get("/lessons")
    async def get_lessons(request: Request, project_id: str = "") -> Dict[str, Any]:
        user = _owner(request)
        if not project_id:
            return {"domains": await asyncio.to_thread(experience.list_domains, user)}
        domain = await asyncio.to_thread(experience.find_domain, project_id, user)
        if domain is None:
            raise HTTPException(404, detail={"error_class": "harness.not_found", "message": "no such project"})
        return {"domain": {"key": domain.key, "name": domain.name, **experience.location(domain)},
                "entries": await asyncio.to_thread(experience.entries, domain)}

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
