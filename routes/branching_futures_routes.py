"""HTTP API for isolated futures and their commit receipts."""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request

from core.middleware import require_admin
from src.auth_helpers import effective_user, get_current_user
from src.branching_futures import service as branching_service
from src.branching_futures.narrative_canon import canon_state, discarded_alternatives
from src.contracts.base import ContractError
from src.owner_identity import effective_storage_owner


def enabled() -> bool:
    try:
        from src.settings import get_setting
        return bool(get_setting("agent_branching_futures", False))
    except Exception:
        return False


def _owner(request: Request) -> str:
    who = effective_user(request) or get_current_user(request) or ""
    owner = str(effective_storage_owner(who) or "").strip()
    if not owner:
        raise HTTPException(403, "a signed-in owner is required")
    return owner


async def _body(request: Request) -> Dict[str, Any]:
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(400, "a JSON object is required")
    if not isinstance(data, dict):
        raise HTTPException(400, "the body must be a JSON object")
    return dict(data)


def _running() -> None:
    if not enabled():
        raise HTTPException(409, "Branching Futures is disabled")


def _error(exc: Exception) -> Dict[str, Any]:
    return {"ok": False, "error": {"code": "invalid_argument",
            "path": str(getattr(exc, "path", "branching")),
            "message": str(getattr(exc, "message", "") or exc)}}


def setup_branching_futures_routes() -> APIRouter:
    router = APIRouter(prefix="/api/futures", tags=["futures"])

    @router.get("/config")
    async def config(request: Request):
        require_admin(request)
        return {"ok": True, "enabled": enabled(),
                "modes": ["plan_only", "simulate", "prototype", "isolated_execute", "shadow", "canary"],
                "real_external_effects_in_branches": False}

    @router.get("/events")
    async def events(request: Request, since: int = 0, limit: int = 200):
        require_admin(request)
        rows = branching_service().events(owner=_owner(request), since=since, limit=limit)
        return {"ok": True, "events": rows, "cursor": rows[-1]["seq"] if rows else since}

    @router.get("")
    async def futures(request: Request, project_id: str = "", status: str = "", limit: int = 100):
        require_admin(request)
        return {"ok": True, "futures": branching_service().futures(
            owner=_owner(request), project_id=project_id, status=status, limit=limit),
            "enabled": enabled()}

    @router.post("")
    async def create(request: Request):
        require_admin(request); _running()
        try:
            return {"ok": True, "future": branching_service().create(
                owner=_owner(request), request=await _body(request))}
        except ContractError as exc:
            return _error(exc)

    @router.get("/{future_id}")
    async def future(request: Request, future_id: str):
        require_admin(request)
        row = branching_service().future(owner=_owner(request), future_id=future_id)
        if row is None:
            raise HTTPException(404, "no such future")
        return {"ok": True, "future": row, "enabled": enabled()}

    @router.post("/{future_id}/branches/{branch_id}/start")
    async def start_branch(request: Request, future_id: str, branch_id: str):
        require_admin(request); _running()
        try:
            return {"ok": True, "branch": branching_service().start_branch(
                owner=_owner(request), future_id=future_id, branch_id=branch_id)}
        except ContractError as exc:
            return _error(exc)

    @router.post("/{future_id}/branches/{branch_id}/result")
    async def result(request: Request, future_id: str, branch_id: str):
        require_admin(request); _running()
        try:
            return {"ok": True, "result": branching_service().submit_result(
                owner=_owner(request), future_id=future_id, branch_id=branch_id,
                result=await _body(request))}
        except ContractError as exc:
            return _error(exc)

    @router.post("/{future_id}/evaluate")
    async def evaluate(request: Request, future_id: str):
        require_admin(request); _running()
        try:
            return {"ok": True, "evaluation": branching_service().evaluate(
                owner=_owner(request), future_id=future_id)}
        except ContractError as exc:
            return _error(exc)

    @router.post("/{future_id}/select")
    async def select(request: Request, future_id: str):
        require_admin(request); _running()
        body = await _body(request)
        try:
            return {"ok": True, "selection": branching_service().select(
                owner=_owner(request), future_id=future_id,
                branch_id=str(body.get("branch_id") or ""),
                rationale=str(body.get("rationale") or ""), authority="human")}
        except ContractError as exc:
            return _error(exc)

    @router.post("/{future_id}/fuse")
    async def fuse(request: Request, future_id: str):
        require_admin(request); _running()
        body = await _body(request)
        try:
            return {"ok": True, "branch": branching_service().fuse(
                owner=_owner(request), future_id=future_id,
                parent_branch_ids=body.get("parent_branch_ids") or [],
                strategy=body.get("strategy") or {})}
        except ContractError as exc:
            return _error(exc)

    @router.post("/{future_id}/revalidate")
    async def revalidate(request: Request, future_id: str):
        require_admin(request); _running()
        body = await _body(request)
        try:
            return {"ok": True, "revalidation": branching_service().revalidate(
                owner=_owner(request), future_id=future_id,
                real_state_fingerprint=str(body.get("real_state_fingerprint") or ""))}
        except ContractError as exc:
            return _error(exc)

    @router.post("/{future_id}/commit")
    async def commit(request: Request, future_id: str):
        require_admin(request); _running()
        body = await _body(request)
        try:
            return {"ok": True, "future": branching_service().commit(
                owner=_owner(request), future_id=future_id,
                real_state_fingerprint=str(body.get("real_state_fingerprint") or ""),
                proof_refs=body.get("proof_refs") or [], approved=body.get("approved") is True)}
        except ContractError as exc:
            return _error(exc)

    @router.get("/canon/state")
    async def canon(request: Request, project_id: str = ""):
        # WRITE-02/WRITE-04 (QA-47): read-only view of what is confirmed for a
        # project — promoted branches only. A discarded alternative never
        # appears here; see `narrative_alternatives` below to inspect it.
        require_admin(request)
        if not project_id:
            raise HTTPException(400, "project_id is required")
        return {"ok": True, "canon": canon_state(
            owner=_owner(request), project_id=project_id, svc=branching_service())}

    @router.get("/canon/alternatives")
    async def narrative_alternatives(request: Request, project_id: str = ""):
        require_admin(request)
        if not project_id:
            raise HTTPException(400, "project_id is required")
        return {"ok": True, "alternatives": discarded_alternatives(
            owner=_owner(request), project_id=project_id, svc=branching_service())}

    @router.post("/{future_id}/cancel")
    async def cancel(request: Request, future_id: str):
        require_admin(request); _running()
        try:
            return {"ok": True, "future": branching_service().cancel(
                owner=_owner(request), future_id=future_id)}
        except ContractError as exc:
            return _error(exc)

    return router
