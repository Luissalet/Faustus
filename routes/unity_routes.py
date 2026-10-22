"""HTTP surface shared with Unity's native chat tool and MCP server."""
from fastapi import APIRouter, Request
from core.middleware import require_admin
from src.unity.tool import UnityTool, execute


def setup_unity_routes():
    router = APIRouter(prefix="/api/unity")

    @router.get("/schema")
    def schema(request: Request):
        require_admin(request)
        return execute({"action": "schema"})

    @router.post("/execute")
    async def operation(request: Request):
        require_admin(request)
        return await UnityTool().execute(await request.json())

    return router
