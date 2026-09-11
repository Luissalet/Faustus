"""routes/side_thread_routes.py — Excursos (side threads), CONTRATO_EXCURSOS
Lote A + CONTRATO_CABLES2 Lote A: ``/api/session/{id}/side-threads``,
``/thought-map``, ``/context-preview``, ``/references*``, ``/materials*``,
``/stale-turns`` and ``/api/side-threads/parents``.

Owner scoping follows ``routes/history/history_routes.py``'s own pattern
(``effective_user`` + ``routes.session_routes._verify_session_owner``) —
a session belonging to someone else answers exactly like one that does not
exist (404), never a 403 that would let a client probe which ids are real.

Errors are flat, CONTRATO_EXCURSOS's own shape: ``{"error", "error_class"}``
at the top level, never nested under ``detail`` — so a client parses every
error from this module the same way regardless of which route raised it.

CONTRATO_CABLES2 F2 deviation: the contract left the choice open between
extending ``PATCH``/``DELETE /references/{wire}`` to also accept material
wires, or giving materials their own ``/materials/{wire_id}``. This module
takes the second option — a material wire carries fields (``note_text``,
``depth`` meaning ``selection``/``full`` rather than ``quote``/``full``)
that would otherwise force ``UpdateReferenceRequest`` to describe two
unrelated shapes under one name; a dedicated ``UpdateMaterialRequest`` and
route keeps each wire kind's request body honest about what it accepts.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core.session_manager import SessionManager
from src.auth_helpers import effective_user
from routes.session_routes import _verify_session_owner
from src.side_threads import (
    SideThreadError,
    add_material,
    add_reference,
    context_preview,
    create_side_thread,
    parents_map,
    remove_material,
    remove_reference,
    stale_turns_map,
    thought_map,
    update_material,
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


class AddMaterialRequest(BaseModel):
    kind: str
    document_id: Optional[str] = None
    depth: str = "selection"
    quotes: Optional[List[str]] = None
    ranges: Optional[List[Dict[str, int]]] = None
    note_text: Optional[str] = Field(None, max_length=8000)


class UpdateMaterialRequest(BaseModel):
    depth: Optional[str] = None
    note_text: Optional[str] = Field(None, max_length=8000)
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

    @router.get("/api/session/{session_id}/stale-turns")
    async def get_stale_turns_route(request: Request, session_id: str) -> Dict[str, Any]:
        """CONTRATO_CABLES2 F1: which of this session's OWN assistant turns
        were written against an earlier version of a wire they used — for
        the transcript to flag them without loading the whole wiring panel.
        """
        _verify_session_owner(request, session_id, session_manager)
        owner = effective_user(request)
        try:
            return stale_turns_map(owner, session_id)
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

    @router.post("/api/session/{session_id}/materials")
    async def add_material_route(request: Request, session_id: str, body: AddMaterialRequest):
        _verify_session_owner(request, session_id, session_manager)
        owner = effective_user(request)
        try:
            result = add_material(
                owner,
                session_id,
                kind=body.kind,
                document_id=body.document_id,
                depth=body.depth,
                quotes=body.quotes,
                ranges=body.ranges,
                note_text=body.note_text,
            )
        except SideThreadError as exc:
            return _from_side_thread_error(exc)
        return JSONResponse(status_code=201, content=result)

    @router.patch("/api/session/{session_id}/materials/{wire_id}")
    async def update_material_route(request: Request, session_id: str, wire_id: str, body: UpdateMaterialRequest):
        _verify_session_owner(request, session_id, session_manager)
        owner = effective_user(request)
        try:
            return update_material(
                owner,
                wire_id,
                depth=body.depth,
                note_text=body.note_text,
                archived=body.archived,
                context_order=body.context_order,
                refresh=body.refresh,
            )
        except SideThreadError as exc:
            return _from_side_thread_error(exc)

    @router.delete("/api/session/{session_id}/materials/{wire_id}")
    async def remove_material_route(request: Request, session_id: str, wire_id: str):
        _verify_session_owner(request, session_id, session_manager)
        owner = effective_user(request)
        removed = remove_material(owner, wire_id)
        if not removed:
            return _error(404, f"Material {wire_id} not found", "excursos.not_found")
        return {"removed": True}

    @router.get("/api/side-threads/parents")
    async def get_parents_map_route(request: Request) -> Dict[str, Any]:
        owner = effective_user(request)
        return {"parents": parents_map(owner)}

    return router
