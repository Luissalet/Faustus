# routes/engine_routes.py
"""`/api/engines` — the local inference engine (llama.cpp's `llama-server`)
managed from the UI (owner's words: "quiero llama-server en la UI... usar
comandos es una mierda"). Built on `src.engines`, itself built on
`src.launch_profiles` (see that module's docstring for why).

Auth follows `routes/connector_routes.py`'s own rule for launch profiles
(principle 4): every route here is `require_human`, including the reads —
there is no legitimate agent use case for configuring or starting a local
process, and `require_human` explicitly refuses Faustus's own internal
agent-tool token. The one read that is `require_admin` (`/status`, plural
and singular) mirrors `/api/launch-profiles/status`'s own comment: a health
probe is a read like any other technical-screen read, not a way to reach the
start/stop surface.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from core.middleware import require_admin, require_human
from src import engines

logger = logging.getLogger(__name__)


def _current_owner(request: Request) -> Optional[str]:
    return getattr(request.state, "current_user", None) or None


class EngineCreateBody(BaseModel):
    name: str
    executable: str
    model_path: str
    ctx_size: int = engines.DEFAULT_CTX
    port: int
    host: str = engines.DEFAULT_HOST
    extra_args: List[str] = []
    description: Optional[str] = None


class EngineUpdateBody(BaseModel):
    name: Optional[str] = None
    executable: Optional[str] = None
    model_path: Optional[str] = None
    ctx_size: Optional[int] = None
    port: Optional[int] = None
    host: Optional[str] = None
    extra_args: Optional[List[str]] = None
    description: Optional[str] = None


def setup_engine_routes() -> APIRouter:
    router = APIRouter(prefix="/api/engines", tags=["engines"])

    # Registered before `/{engine_id}` so "status"/"discover" are never
    # swallowed as an engine id, same ordering note as launch-profiles'.
    @router.get("/status")
    async def all_statuses(request: Request) -> Dict[str, Dict[str, Any]]:
        require_admin(request)
        out: Dict[str, Dict[str, Any]] = {}
        for engine in engines.list_engines():
            out[engine["id"]] = await engines.status_engine(engine["id"])
        return out

    @router.get("/discover")
    async def discover(request: Request, port: int, host: str = engines.DEFAULT_HOST) -> Dict[str, Any]:
        require_human(request)
        return await engines.discover_from_port(port, host=host)

    @router.get("")
    def list_engines_route(request: Request) -> List[Dict[str, Any]]:
        require_human(request)
        return engines.list_engines()

    @router.get("/{engine_id}")
    def get_engine_route(engine_id: str, request: Request) -> Dict[str, Any]:
        require_human(request)
        engine = engines.get_engine(engine_id)
        if engine is None:
            raise HTTPException(404, "Engine not found")
        return engine

    @router.get("/{engine_id}/status")
    async def get_engine_status_route(engine_id: str, request: Request) -> Dict[str, Any]:
        require_admin(request)
        if engines.get_engine(engine_id) is None:
            raise HTTPException(404, "Engine not found")
        return await engines.status_engine(engine_id)

    @router.post("")
    def create_engine_route(body: EngineCreateBody, request: Request) -> Dict[str, Any]:
        require_human(request)
        owner = _current_owner(request)
        try:
            return engines.create_engine(owner=owner, **body.model_dump())
        except engines.EngineValidationError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.patch("/{engine_id}")
    def update_engine_route(engine_id: str, body: EngineUpdateBody, request: Request) -> Dict[str, Any]:
        require_human(request)
        fields = {k: v for k, v in body.model_dump().items() if v is not None}
        try:
            engine = engines.update_engine(engine_id, **fields)
        except engines.EngineValidationError as exc:
            raise HTTPException(400, str(exc)) from exc
        if engine is None:
            raise HTTPException(404, "Engine not found")
        return engine

    @router.delete("/{engine_id}")
    def delete_engine_route(engine_id: str, request: Request) -> Dict[str, Any]:
        require_human(request)
        if not engines.delete_engine(engine_id):
            raise HTTPException(404, "Engine not found")
        return {"deleted": True}

    @router.post("/{engine_id}/start")
    async def start_engine_route(engine_id: str, request: Request) -> Dict[str, Any]:
        require_human(request)
        if engines.get_engine(engine_id) is None:
            raise HTTPException(404, "Engine not found")
        client_host = request.client.host if request.client else None
        return await engines.start_engine(engine_id, request_client_host=client_host)

    @router.post("/{engine_id}/stop")
    async def stop_engine_route(engine_id: str, request: Request) -> Dict[str, Any]:
        require_human(request)
        if engines.get_engine(engine_id) is None:
            raise HTTPException(404, "Engine not found")
        return await engines.stop_engine(engine_id)

    @router.post("/{engine_id}/verify")
    async def verify_engine_route(engine_id: str, request: Request) -> Dict[str, Any]:
        require_human(request)
        if engines.get_engine(engine_id) is None:
            raise HTTPException(404, "Engine not found")
        return await engines.verify_engine(engine_id)

    return router
