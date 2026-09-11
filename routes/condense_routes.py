"""routes/condense_routes.py — manual condense of a turn range,
CONTRATO_CABLES2 F3 Lote A.

``GET /api/session/{id}/condense/preview``, ``POST /api/session/{id}/
condense`` and ``POST /api/session/{id}/condense/{index}/expand``. Owner
scoping follows ``routes/side_thread_routes.py``'s own pattern
(``effective_user`` + ``routes.session_routes._verify_session_owner``) — a
session belonging to someone else answers exactly like one that does not
exist (404). Errors are flat: ``{"error", "error_class"}`` at the top level,
``error_class`` always ``condense.<reason>``.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from core.session_manager import SessionManager
from src.auth_helpers import effective_user
from routes.session_routes import _verify_session_owner
from src.condense import CondenseError, condense, expand, preview

logger = logging.getLogger(__name__)


def _error(status: int, message: str, error_class: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": message, "error_class": error_class})


def _from_condense_error(exc: CondenseError) -> JSONResponse:
    return _error(exc.status, str(exc), exc.error_class)


class CondenseRequest(BaseModel):
    start: int
    end: int


def setup_condense_routes(session_manager: SessionManager) -> APIRouter:
    """Build the condense router bound to ``session_manager`` — built fresh
    per call (not a module-level router), matching
    ``setup_side_thread_routes``'s own reason: a second call in tests never
    piles duplicate routes onto a shared object."""
    router = APIRouter(tags=["condense"])

    @router.get("/api/session/{session_id}/condense/preview")
    async def condense_preview_route(request: Request, session_id: str, start: int, end: int) -> Dict[str, Any]:
        _verify_session_owner(request, session_id, session_manager)
        owner = effective_user(request)
        try:
            return preview(session_manager, owner, session_id, start, end)
        except CondenseError as exc:
            return _from_condense_error(exc)

    @router.post("/api/session/{session_id}/condense")
    async def condense_route(request: Request, session_id: str, body: CondenseRequest):
        _verify_session_owner(request, session_id, session_manager)
        owner = effective_user(request)
        try:
            return await condense(session_manager, owner, session_id, body.start, body.end)
        except CondenseError as exc:
            return _from_condense_error(exc)

    @router.post("/api/session/{session_id}/condense/{index}/expand")
    async def condense_expand_route(request: Request, session_id: str, index: int):
        _verify_session_owner(request, session_id, session_manager)
        owner = effective_user(request)
        try:
            return expand(session_manager, owner, session_id, index)
        except CondenseError as exc:
            return _from_condense_error(exc)

    return router
