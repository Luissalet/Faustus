"""HTTP surface shared by the turn summary and external MCP clients."""
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
import httpx

from core.middleware import require_admin
from src.auth_helpers import effective_user
from src import task_walkthrough


def setup_task_walkthrough_routes():
    router = APIRouter(tags=["delivery"])

    @router.post("/api/task-walkthrough")
    async def start(request: Request):
        require_admin(request)
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, "Give a JSON object")
        result = await task_walkthrough.run(effective_user(request), body)
        return JSONResponse(result, status_code=400 if result.get("error") else 200)

    @router.get("/api/task-walkthrough/{job_id}/video")
    async def video(job_id: str, request: Request):
        require_admin(request)
        try:
            response = await task_walkthrough.video(effective_user(request), job_id, request.headers.get("range"))
        except (ValueError, KeyError, httpx.HTTPError) as exc:
            raise HTTPException(400, str(exc)) from exc
        headers = {k: v for k, v in response.headers.items() if k in ("content-range", "accept-ranges", "content-length")}
        return Response(response.content, status_code=response.status_code, media_type="video/mp4", headers=headers)

    return router
