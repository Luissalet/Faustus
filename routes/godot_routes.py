"""The normal HTTP surface of the native engine operations."""
from fastapi import APIRouter, Request, HTTPException
from core.middleware import require_admin
from src.auth_helpers import effective_user
from src.godot.tool import GodotTool, execute


def setup_godot_routes():
    router = APIRouter(prefix="/api/godot", tags=["Godot"])

    @router.get("/schema")
    def schema(request: Request):
        require_admin(request)
        return execute({"action":"schema"})

    @router.post("/execute")
    async def operation(request: Request):
        require_admin(request)
        try: body = await request.json()
        except ValueError: raise HTTPException(400, "Valid JSON required")
        if not isinstance(body, dict): raise HTTPException(400, "JSON object required")
        if body.get("session_id"):
            from routes.session_routes import _verify_session_owner
            _verify_session_owner(request, body["session_id"])
        return await GodotTool().execute(body, {"owner":str(effective_user(request) or ""), "session_id":body.get("session_id", "")})

    return router
