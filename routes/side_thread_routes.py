"""routes/side_thread_routes.py — Excursos (side threads), CONTRATO_EXCURSOS
Lote A: ``/api/session/{id}/side-threads``, ``/thought-map``,
``/context-preview``, ``/references*`` and ``/api/side-threads/parents``.

Owner scoping follows ``routes/history/history_routes.py``'s own pattern
(``effective_user`` + ``routes.session_routes._verify_session_owner``) —
a session belonging to someone else answers exactly like one that does not
exist (404), never a 403 that would let a client probe which ids are real.

Errors are flat, CONTRATO_EXCURSOS's own shape: ``{"error", "error_class"}``
at the top level, never nested under ``detail`` — so a client parses every
error from this module the same way regardless of which route raised it.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core.session_manager import SessionManager
from src.auth_helpers import effective_user
from routes.session_routes import _verify_session_owner
from src.side_threads import (
    SideThreadError,
    add_reference,
    context_preview,
    create_side_thread,
    parents_map,
    remove_reference,
    thought_map,
    update_reference,
    wires_for,
)

logger = logging.getLogger(__name__)


def _error(status: int, message: str, error_class: str) -> JSONResponse:
    """The flat ``{"error", "error_class"}`` body CONTRATO_EXCURSOS requires
    — ``error`` (not ``detail``) at the top level next to ``error_class``."""
    return JSONResponse(status_code=status, content={"error": message, "error_class": error_class})


def _from_side_thread_error(exc: SideThreadError) -> JSONResponse:
    return _error(exc.status, str(exc), exc.error_class)


class CreateSideThreadRequest(BaseModel):
    anchor_index: int
    passage: Optional[str] = Field(None, max_length=2000)
    question: Optional[str] = None


class AddReferenceRequest(BaseModel):
    source_session_id: str
    depth: str = "quote"


class UpdateReferenceRequest(BaseModel):
    depth: Optional[str] = None
    archived: Optional[bool] = None
    context_order: Optional[int] = None
    refresh: bool = False


def setup_side_thread_routes(session_manager: SessionManager) -> APIRouter:
    """Build the excursos router bound to ``session_manager`` — built fresh
    per call (not a module-level router) so a second call in tests never
    piles duplicate routes onto a shared object."""
    router = APIRouter(tags=["side-threads"])

    @router.post("/api/session/{session_id}/side-threads")
    async def create_side_thread_route(request: Request, session_id: str, body: CreateSideThreadRequest):
        _verify_session_owner(request, session_id, session_manager)
        owner = effective_user(request)
        try:
            result = create_side_thread(
                session_manager,
                owner,
                session_id,
                body.anchor_index,
                passage=body.passage,
                question=body.question,
            )
        except SideThreadError as exc:
            return _from_side_thread_error(exc)
        return JSONResponse(
            status_code=201,
            content={
                "session_id": result["session_id"],
                "wire": result["wire"],
                "question": body.question,
            },
        )

    @router.get("/api/session/{session_id}/side-threads")
    async def get_side_threads_route(request: Request, session_id: str) -> Dict[str, Any]:
        _verify_session_owner(request, session_id, session_manager)
        owner = effective_user(request)
        try:
            return wires_for(owner, session_id)
        except SideThreadError as exc:
            return _from_side_thread_error(exc)

    @router.get("/api/session/{session_id}/thought-map")
    async def get_thought_map_route(request: Request, session_id: str) -> Dict[str, Any]:
        _verify_session_owner(request, session_id, session_manager)
        owner = effective_user(request)
        try:
            return thought_map(owner, session_id)
        except SideThreadError as exc:
            return _from_side_thread_error(exc)

    @router.get("/api/session/{session_id}/context-preview")
    async def get_context_preview_route(request: Request, session_id: str) -> Dict[str, Any]:
        _verify_session_owner(request, session_id, session_manager)
        owner = effective_user(request)
        try:
            return context_preview(session_manager, owner, session_id)
        except SideThreadError as exc:
            return _from_side_thread_error(exc)

    @router.post("/api/session/{session_id}/references")
    async def add_reference_route(request: Request, session_id: str, body: AddReferenceRequest):
        _verify_session_owner(request, session_id, session_manager)
        owner = effective_user(request)
        try:
            result = add_reference(owner, body.source_session_id, session_id, depth=body.depth)
        except SideThreadError as exc:
            return _from_side_thread_error(exc)
        return result

    @router.patch("/api/session/{session_id}/references/{wire_id}")
    async def update_reference_route(request: Request, session_id: str, wire_id: str, body: UpdateReferenceRequest):
        _verify_session_owner(request, session_id, session_manager)
        owner = effective_user(request)
        try:
            return update_reference(
                owner,
                wire_id,
                depth=body.depth,
                archived=body.archived,
                context_order=body.context_order,
                refresh=body.refresh,
            )
        except SideThreadError as exc:
            return _from_side_thread_error(exc)

    @router.delete("/api/session/{session_id}/references/{wire_id}")
    async def remove_reference_route(request: Request, session_id: str, wire_id: str):
        _verify_session_owner(request, session_id, session_manager)
        owner = effective_user(request)
        removed = remove_reference(owner, wire_id)
        if not removed:
            return _error(404, f"Reference {wire_id} not found", "excursos.not_found")
        return {"removed": True}

    @router.get("/api/side-threads/parents")
    async def get_parents_map_route(request: Request) -> Dict[str, Any]:
        owner = effective_user(request)
        return {"parents": parents_map(owner)}

    return router
