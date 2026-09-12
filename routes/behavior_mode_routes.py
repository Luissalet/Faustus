"""routes/behavior_mode_routes.py — CONTRATO_MODOS Lote A.

Behaviour-mode catalog + per-session override, mirroring
`routes/side_thread_routes.py`'s own conventions: flat `{"error",
"error_class"}` bodies (never nested under `detail`), and session-scoped
routes go through `routes.session_routes._verify_session_owner` so a
session that belongs to someone else answers exactly like one that does
not exist (404), never a 403 that would let a client probe which ids are
real.

    GET    /api/behavior-modes                 -> {modes, default}
    POST   /api/behavior-modes                 -> {mode}   (create/update one of the caller's own)
    DELETE /api/behavior-modes/{id}             -> {ok}
    POST   /api/behavior-modes/default          -> {default}   (admin only — the global setting)
    GET    /api/session/{id}/behavior-mode      -> {mode, effective}
    POST   /api/session/{id}/behavior-mode      -> {mode}
    POST   /api/behavior-modes/check            -> check_response(mode, text)

Every route here only ever changes HOW Faustus talks for a caller who asks
for that, never what an in-flight chat turn is doing — see
`src/behavior_modes.py`'s module docstring and `docs/api/behavior_modes.md`
for the full story of why a mode can't touch `UNTRUSTED_CONTEXT_POLICY`.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core.database import get_session_behavior_mode, set_session_behavior_mode
from core.middleware import require_admin
from core.session_manager import SessionManager
from routes.session_routes import _verify_session_owner
from src import behavior_modes
from src.auth_helpers import effective_user
from src.settings import SettingsError, get_setting, update_settings

logger = logging.getLogger(__name__)


def _error(status: int, message: str, error_class: str) -> JSONResponse:
    """Flat `{"error", "error_class"}` body — see module docstring."""
    return JSONResponse(status_code=status, content={"error": message, "error_class": error_class})


def _from_mode_error(exc: behavior_modes.ModeError) -> JSONResponse:
    code = str(exc) or "invalid"
    if code == "builtin":
        return _error(409, "That id belongs to a built-in mode.", "modes.builtin")
    if code == "not_found":
        return _error(404, "No such mode.", "modes.not_found")
    return _error(400, "Invalid behaviour mode.", "modes.invalid")


class SaveModeRequest(BaseModel):
    id: str
    name: Dict[str, str]
    description: Dict[str, str] = Field(default_factory=dict)
    prompt: str = ""
    checks: Dict[str, Any] = Field(default_factory=dict)


class SetDefaultRequest(BaseModel):
    mode: str


class SetSessionModeRequest(BaseModel):
    mode: Optional[str] = None


class CheckRequest(BaseModel):
    mode: str
    text: str = ""


def setup_behavior_mode_routes(session_manager: SessionManager) -> APIRouter:
    """Build the behaviour-modes router bound to `session_manager` — built
    fresh per call (not a module-level router), same reason
    `setup_side_thread_routes` gives: a second call in tests must never pile
    duplicate routes onto a shared object."""
    router = APIRouter(tags=["behavior-modes"])

    @router.get("/api/behavior-modes")
    async def list_behavior_modes(request: Request) -> Dict[str, Any]:
        owner = effective_user(request)
        modes = behavior_modes.list_modes(owner)
        return {
            "modes": [m.to_dict() for m in modes],
            "default": get_setting("behavior_mode_default", "default"),
        }

    @router.post("/api/behavior-modes")
    async def save_behavior_mode(request: Request, body: SaveModeRequest):
        owner = effective_user(request)
        try:
            mode = behavior_modes.save_user_mode(owner, body.model_dump())
        except behavior_modes.ModeError as exc:
            return _from_mode_error(exc)
        return {"mode": mode.to_dict()}

    @router.delete("/api/behavior-modes/{mode_id}")
    async def delete_behavior_mode(request: Request, mode_id: str):
        owner = effective_user(request)
        try:
            behavior_modes.delete_user_mode(owner, mode_id)
        except behavior_modes.ModeError as exc:
            return _from_mode_error(exc)
        return {"ok": True}

    @router.post("/api/behavior-modes/default")
    async def set_default_behavior_mode(request: Request, body: SetDefaultRequest):
        require_admin(request)
        # The global fallback must be something every user can actually get
        # (nobody's private mode is guaranteed to exist for everybody else),
        # so only a built-in id is accepted here.
        if body.mode not in behavior_modes.load_builtin():
            return _error(400, f"Unknown built-in mode {body.mode!r}.", "modes.invalid")
        try:
            update_settings({"behavior_mode_default": body.mode})
        except SettingsError as exc:
            return _error(400, str(exc), "modes.invalid")
        return {"default": body.mode}

    @router.get("/api/session/{session_id}/behavior-mode")
    async def get_session_behavior_mode_route(request: Request, session_id: str) -> Dict[str, Any]:
        _verify_session_owner(request, session_id, session_manager)
        owner = effective_user(request)
        stored = get_session_behavior_mode(session_id)
        effective = behavior_modes.resolve(
            requested=None,
            session_mode=stored,
            default_setting=get_setting("behavior_mode_default"),
            owner=owner,
        )
        return {"mode": stored, "effective": effective.id}

    @router.post("/api/session/{session_id}/behavior-mode")
    async def set_session_behavior_mode_route(request: Request, session_id: str, body: SetSessionModeRequest):
        _verify_session_owner(request, session_id, session_manager)
        owner = effective_user(request)
        mode_id = body.mode
        if mode_id is not None and behavior_modes.get_mode(mode_id, owner) is None:
            return _error(400, f"Unknown behaviour mode {mode_id!r}.", "modes.invalid")
        set_session_behavior_mode(session_id, mode_id)
        return {"mode": mode_id}

    @router.post("/api/behavior-modes/check")
    async def check_behavior_mode_text(request: Request, body: CheckRequest):
        owner = effective_user(request)
        mode = behavior_modes.get_mode(body.mode, owner)
        if mode is None:
            return _error(404, f"Unknown behaviour mode {body.mode!r}.", "modes.not_found")
        return behavior_modes.check_response(mode, body.text)

    return router
