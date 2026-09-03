"""Command guard admin routes — /api/command-guard/* (FAUSTUS).

Explain a command's classification, read the hash-chained decision receipts,
and manage the allowlist. Everything here is admin-only: the allowlist is a
standing authority downgrade, and the receipts log names commands.

The two reads (``/explain`` and ``/log``) also answer in robot mode
(``?robot=1`` / ``?format=toon``, src/robot_envelope.py): a coordinating model
pre-checking a command before it dispatches workers gets the standard envelope
— and the receipts tail, which is the tabular payload TOON compresses best.
Without those query parameters the answers are unchanged.
"""

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from core.middleware import require_admin
from src import robot_envelope as robot

logger = logging.getLogger(__name__)


class AllowlistAdd(BaseModel):
    pattern: str
    kind: str = "exact"
    reason: str = ""
    ttl_hours: Optional[float] = None


class AllowlistRemove(BaseModel):
    pattern: Optional[str] = None
    index: Optional[int] = None


def setup_command_guard_routes() -> APIRouter:
    router = APIRouter(tags=["command-guard"])

    @router.get("/api/command-guard/explain")
    async def explain_command(request: Request, command: str = "") -> Dict[str, Any]:
        """Full classification trace + allowlist hit + current mode."""
        def payload() -> Dict[str, Any]:
            require_admin(request)
            from src import command_guard
            from src.tool_capabilities import command_guard_mode, _command_guard_packs
            if not command.strip():
                raise HTTPException(status_code=400, detail="command is required")
            packs = _command_guard_packs()
            report = command_guard.explain(command, packs=packs)
            entry = command_guard.is_allowlisted(command)
            return {
                "status": "success",
                "mode": command_guard_mode(),
                "packs": sorted(packs),
                "allowlisted": entry,
                **report,
            }
        if robot.wants(request):
            return await robot.reply(request, payload)
        return payload()

    @router.get("/api/command-guard/log")
    async def guard_log(request: Request, limit: int = 100) -> Dict[str, Any]:
        """Receipts tail plus a full chain verification."""
        def payload() -> Dict[str, Any]:
            require_admin(request)
            from src import command_guard
            return {
                "status": "success",
                "receipts": command_guard.tail_receipts(limit),
                "chain": command_guard.verify_chain(),
            }
        if robot.wants(request):
            return await robot.reply(request, payload)
        return payload()

    @router.get("/api/command-guard/allowlist")
    async def get_allowlist(request: Request) -> Dict[str, Any]:
        require_admin(request)
        from src import command_guard
        return {"status": "success", "allow": command_guard.list_allowlist()}

    @router.post("/api/command-guard/allowlist")
    async def add_allowlist(request: Request, body: AllowlistAdd) -> Dict[str, Any]:
        require_admin(request)
        from src import command_guard
        owner = getattr(request.state, "current_user", None) or ""
        try:
            entry = command_guard.add_allowlist_entry(
                body.pattern,
                kind=body.kind,
                reason=body.reason,
                added_by=str(owner),
                ttl_hours=body.ttl_hours,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"status": "success", "entry": entry}

    @router.delete("/api/command-guard/allowlist")
    async def delete_allowlist(request: Request, body: AllowlistRemove) -> Dict[str, Any]:
        require_admin(request)
        from src import command_guard
        if body.pattern is None and body.index is None:
            raise HTTPException(status_code=400, detail="pattern or index is required")
        removed = command_guard.remove_allowlist_entry(
            pattern=body.pattern, index=body.index
        )
        if not removed:
            raise HTTPException(status_code=404, detail="no matching allowlist entry")
        return {"status": "success", "removed": True}

    return router
