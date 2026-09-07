"""Human-managed credentials; scripts see only explicit approved bindings."""
import asyncio
import json

from fastapi import APIRouter, HTTPException, Request
from core.middleware import require_human
from src.owner_identity import effective_storage_owner
from src.workflows import credentials


def _owner(request):
    require_human(request)
    owner = effective_storage_owner(getattr(request.state, "current_user", None))
    if not owner:
        raise HTTPException(403, "A signed-in owner is required")
    return owner


async def _body(request):
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > 128 * 1024:
            raise HTTPException(413, "Credential request is too large")
    try:
        result = json.loads(data)
    except (ValueError, UnicodeError):
        raise HTTPException(400, "Expected a JSON object") from None
    if not isinstance(result, dict) or set(result) - {"value", "expected_revision"}:
        raise HTTPException(400, "Only value and expected_revision are accepted")
    return result


def setup_workflow_credentials_routes():
    router = APIRouter(prefix="/credentials", tags=["workflow credentials"])

    @router.get("")
    def list_credentials(request: Request):
        return {"credentials": credentials.list_metadata(_owner(request))}

    @router.put("/{name}")
    async def put_credential(name: str, request: Request):
        owner = _owner(request)
        body = await _body(request)
        try:
            return await asyncio.to_thread(credentials.put, owner, name, body.get("value"),
                                           expected_revision=body.get("expected_revision"))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @router.delete("/{name}")
    async def delete_credential(name: str, request: Request):
        owner = _owner(request)
        body = await _body(request)
        try:
            removed = await asyncio.to_thread(credentials.remove, owner, name,
                                              expected_revision=body.get("expected_revision"))
            return {"removed": removed}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    return router
