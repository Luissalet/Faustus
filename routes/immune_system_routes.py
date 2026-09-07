"""Owner-scoped HTTP API for capability health and governed repair."""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request

from core.middleware import require_admin
from src.auth_helpers import effective_user, get_current_user
from src.contracts.base import ContractError
from src.immune_system import service as immune_service
from src.owner_identity import effective_storage_owner


def enabled() -> bool:
    try:
        from src.settings import get_setting
        return bool(get_setting("agent_immune_system", False))
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
        raise HTTPException(409, "Immune System is disabled")


def _error(exc: Exception) -> Dict[str, Any]:
    return {"ok": False, "error": {"code": "invalid_argument",
            "path": str(getattr(exc, "path", "immune")),
            "message": str(getattr(exc, "message", "") or exc)}}


def setup_immune_system_routes() -> APIRouter:
    router = APIRouter(prefix="/api/immune", tags=["immune"])

    @router.get("/config")
    async def config(request: Request):
        require_admin(request)
        return {"ok": True, "enabled": enabled(), "promotion_requires_proof": True,
                "quarantine_blocks_automatic_selection": True}

    @router.get("/events")
    async def events(request: Request, since: int = 0, limit: int = 200):
        require_admin(request)
        rows = immune_service().events(owner=_owner(request), since=since, limit=limit)
        return {"ok": True, "events": rows, "cursor": rows[-1]["seq"] if rows else since}

    @router.get("/assets")
    async def assets(request: Request, project_id: str = "", status: str = "", limit: int = 200):
        require_admin(request)
        return {"ok": True, "assets": immune_service().assets(
            owner=_owner(request), project_id=project_id, status=status, limit=limit),
            "enabled": enabled()}

    @router.post("/assets")
    async def register(request: Request):
        require_admin(request); _running()
        try:
            return {"ok": True, "asset": immune_service().register_asset(
                owner=_owner(request), request=await _body(request))}
        except ContractError as exc:
            return _error(exc)

    @router.get("/assets/{asset_id:path}")
    async def asset(request: Request, asset_id: str):
        require_admin(request)
        row = immune_service().asset(owner=_owner(request), asset_id=asset_id)
        if row is None:
            raise HTTPException(404, "no such asset")
        return {"ok": True, "asset": row, "enabled": enabled()}

    @router.post("/assets/{asset_id:path}/assess")
    async def assess(request: Request, asset_id: str):
        require_admin(request); _running()
        try:
            return {"ok": True, "asset": immune_service().assess(
                owner=_owner(request), asset_id=asset_id, assessment=await _body(request))}
        except ContractError as exc:
            return _error(exc)

    @router.post("/assets/{asset_id:path}/failures")
    async def failure(request: Request, asset_id: str):
        require_admin(request); _running()
        try:
            return {"ok": True, "incident": immune_service().report_failure(
                owner=_owner(request), asset_id=asset_id, failure=await _body(request))}
        except ContractError as exc:
            return _error(exc)

    @router.post("/assets/{asset_id:path}/quarantine")
    async def quarantine(request: Request, asset_id: str):
        require_admin(request); _running()
        body = await _body(request)
        try:
            return {"ok": True, "asset": immune_service().quarantine(
                owner=_owner(request), asset_id=asset_id, reason=str(body.get("reason") or ""))}
        except ContractError as exc:
            return _error(exc)

    @router.post("/assets/{asset_id:path}/unquarantine")
    async def unquarantine(request: Request, asset_id: str):
        require_admin(request); _running()
        body = await _body(request)
        try:
            return {"ok": True, "asset": immune_service().unquarantine(
                owner=_owner(request), asset_id=asset_id, reason=str(body.get("reason") or ""))}
        except ContractError as exc:
            return _error(exc)

    @router.get("/incidents")
    async def incidents(request: Request, asset_id: str = "", status: str = "", limit: int = 200):
        require_admin(request)
        return {"ok": True, "incidents": immune_service().incidents(
            owner=_owner(request), asset_id=asset_id, status=status, limit=limit),
            "enabled": enabled()}

    @router.get("/incidents/{incident_id}")
    async def incident(request: Request, incident_id: str):
        require_admin(request)
        row = immune_service().incident(owner=_owner(request), incident_id=incident_id)
        if row is None:
            raise HTTPException(404, "no such incident")
        return {"ok": True, "incident": row, "enabled": enabled()}

    @router.post("/assets/{asset_id:path}/repairs")
    async def repair(request: Request, asset_id: str):
        require_admin(request); _running()
        try:
            return {"ok": True, "repair": immune_service().create_repair(
                owner=_owner(request), asset_id=asset_id, request=await _body(request))}
        except ContractError as exc:
            return _error(exc)

    @router.get("/repairs/{repair_id}")
    async def get_repair(request: Request, repair_id: str):
        require_admin(request)
        row = immune_service().repair(owner=_owner(request), repair_id=repair_id)
        if row is None:
            raise HTTPException(404, "no such repair")
        return {"ok": True, "repair": row, "enabled": enabled()}

    @router.post("/repairs/{repair_id}/{action}")
    async def transition(request: Request, repair_id: str, action: str):
        require_admin(request); _running()
        try:
            return {"ok": True, "repair": immune_service().transition_repair(
                owner=_owner(request), repair_id=repair_id, action=action,
                evidence=await _body(request))}
        except ContractError as exc:
            return _error(exc)

    return router
