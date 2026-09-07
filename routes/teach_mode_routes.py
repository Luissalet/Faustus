"""HTTP surface for Modo Enséñame (plan 8)."""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request

from core.middleware import require_admin
from src.auth_helpers import effective_user, get_current_user
from src.contracts.base import ContractError
from src.owner_identity import effective_storage_owner
from src.teach_mode import service as teach_service_module


def enabled() -> bool:
    try:
        from src.settings import get_setting
        return bool(get_setting("agent_teach_mode", False))
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


def _error(exc: Exception) -> Dict[str, Any]:
    return {"ok": False, "error": {"code": "invalid_argument",
            "path": str(getattr(exc, "path", "teach")),
            "message": str(getattr(exc, "message", "") or exc)}}


def _running() -> None:
    if not enabled():
        raise HTTPException(409, "Modo Enséñame is disabled")


def setup_teach_mode_routes() -> APIRouter:
    router = APIRouter(prefix="/api/teach", tags=["teach"])

    @router.get("/config")
    async def config(request: Request):
        require_admin(request)
        return {"ok": True, "enabled": enabled(),
                "capture": "semantic tool events", "installation_requires_approval": True}

    @router.get("/events")
    async def events(request: Request, since: int = 0, limit: int = 200):
        require_admin(request)
        rows = teach_service_module().events(owner=_owner(request), since=since, limit=limit)
        return {"ok": True, "events": rows, "cursor": rows[-1]["seq"] if rows else since}

    @router.get("/demonstrations")
    async def demonstrations(request: Request, project_id: str = "", session_id: str = "",
                             status: str = "", limit: int = 100):
        require_admin(request)
        return {"ok": True, "demonstrations": teach_service_module().list(
            owner=_owner(request), project_id=project_id, session_id=session_id,
            status=status, limit=limit), "enabled": enabled()}

    @router.post("/demonstrations")
    async def start(request: Request):
        require_admin(request); _running()
        body = await _body(request)
        try:
            row = teach_service_module().start(owner=_owner(request), request=body)
            return {"ok": True, "demonstration": row}
        except ContractError as exc:
            return _error(exc)

    @router.get("/demonstrations/{demonstration_id}")
    async def demonstration(request: Request, demonstration_id: str):
        require_admin(request)
        row = teach_service_module().get(owner=_owner(request), demonstration_id=demonstration_id)
        if row is None:
            raise HTTPException(404, "no such demonstration")
        return {"ok": True, "demonstration": row, "enabled": enabled()}

    @router.post("/demonstrations/{demonstration_id}/observations")
    async def observe(request: Request, demonstration_id: str):
        require_admin(request); _running()
        try:
            row = teach_service_module().observe(owner=_owner(request), demonstration_id=demonstration_id,
                                                 observation=await _body(request))
            return {"ok": True, "observation": row}
        except ContractError as exc:
            return _error(exc)

    @router.post("/demonstrations/{demonstration_id}/stop")
    async def stop(request: Request, demonstration_id: str):
        require_admin(request); _running()
        try:
            return {"ok": True, "demonstration": teach_service_module().stop(
                owner=_owner(request), demonstration_id=demonstration_id)}
        except ContractError as exc:
            return _error(exc)

    @router.post("/demonstrations/{demonstration_id}/pause")
    async def pause(request: Request, demonstration_id: str):
        require_admin(request); _running()
        try:
            return {"ok": True, "demonstration": teach_service_module().pause(
                owner=_owner(request), demonstration_id=demonstration_id)}
        except ContractError as exc:
            return _error(exc)

    @router.post("/demonstrations/{demonstration_id}/resume")
    async def resume(request: Request, demonstration_id: str):
        require_admin(request); _running()
        try:
            return {"ok": True, "demonstration": teach_service_module().resume(
                owner=_owner(request), demonstration_id=demonstration_id)}
        except ContractError as exc:
            return _error(exc)

    @router.get("/demonstrations/{demonstration_id}/trace")
    async def trace(request: Request, demonstration_id: str):
        require_admin(request)
        try:
            return {"ok": True, "trace": teach_service_module().trace(
                owner=_owner(request), demonstration_id=demonstration_id)}
        except ContractError as exc:
            return _error(exc)

    @router.post("/demonstrations/{demonstration_id}/cancel")
    async def cancel(request: Request, demonstration_id: str):
        require_admin(request); _running()
        try:
            return {"ok": True, "demonstration": teach_service_module().stop(
                owner=_owner(request), demonstration_id=demonstration_id, cancelled=True)}
        except ContractError as exc:
            return _error(exc)

    @router.post("/demonstrations/{demonstration_id}/compile")
    async def compile_demonstration(request: Request, demonstration_id: str):
        require_admin(request); _running()
        try:
            return {"ok": True, "procedure": teach_service_module().compile(
                owner=_owner(request), demonstration_id=demonstration_id)}
        except ContractError as exc:
            return _error(exc)

    @router.get("/procedures")
    async def procedures(request: Request, project_id: str = "", status: str = "", limit: int = 100):
        require_admin(request)
        return {"ok": True, "procedures": teach_service_module().procedures(
            owner=_owner(request), project_id=project_id, status=status, limit=limit),
            "enabled": enabled()}

    @router.get("/procedures/{procedure_id}")
    async def procedure(request: Request, procedure_id: str):
        require_admin(request)
        row = teach_service_module().procedure(owner=_owner(request), procedure_id=procedure_id)
        if row is None:
            raise HTTPException(404, "no such procedure")
        return {"ok": True, "procedure": row, "enabled": enabled()}

    @router.post("/procedures/{procedure_id}/{action}")
    async def transition(request: Request, procedure_id: str, action: str):
        require_admin(request); _running()
        body = await _body(request)
        try:
            row = teach_service_module().transition(owner=_owner(request), procedure_id=procedure_id,
                                                    action=action, evidence=body)
            return {"ok": True, "procedure": row}
        except ContractError as exc:
            return _error(exc)

    return router
