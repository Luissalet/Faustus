"""Inbox over native approvals. The same person-only decision boundary."""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, StrictBool

from core.middleware import require_human
from routes.approvals_routes import _read_owner
from src import approval_inbox as inbox


def _owner(request):
    from src.auth_helpers import storage_owner_for_request
    from src.chat_bridges.turn import resolve_owner
    from src.owner_identity import single_user_mode
    owner = _read_owner(request, storage_owner_for_request(request) or resolve_owner())
    if single_user_mode() and not getattr(request.state, "api_token", False):
        return None  # the existing no-login mode owns every historical bucket
    return owner


class Answer(BaseModel):
    granted: StrictBool
    remember: StrictBool = False


class RuleChange(BaseModel):
    granted: StrictBool


def setup_approval_inbox_routes():
    router = APIRouter(prefix="/api/approval-inbox", tags=["approvals"])

    @router.get("")
    def read(request: Request):
        owner = _owner(request)
        return {"pending": inbox.pending(owner), "rules": inbox.rules(owner)}

    @router.post("/{code}/decide")
    async def decide(code: str, body: Answer, request: Request):
        require_human(request)
        owner = _owner(request)
        try:
            result = await inbox.decide(owner, code, granted=body.granted, remember=body.remember)
        except (ValueError, OSError) as exc:
            raise HTTPException(409, str(exc)) from exc
        if not result.get("ok"):
            raise HTTPException(409, result)
        return result

    @router.put("/rules/{pattern}")
    def edit(pattern: str, body: RuleChange, request: Request):
        require_human(request)
        try:
            inbox.edit_rule(_owner(request), pattern, body.granted)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {"ok": True}

    @router.delete("/rules/{pattern}")
    def forget(pattern: str, request: Request):
        require_human(request)
        try:
            inbox.edit_rule(_owner(request), pattern)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {"ok": True}

    return router
