"""routes/chat_mode_routes.py - lean mode, pinned per chat.

    GET  /api/sessions/{id}/mode   -> {mode, stored, default, pinned, pinned_at,
                                       pinned_sha256, pinned_keys, drops}
    POST /api/sessions/{id}/mode   {"mode": "lean"|"normal"} -> the same view

`GET` never pins anything: a chat that has not chosen reports the global
default (`agent_default_chat_mode`) as its effective mode with `stored: false`.
`POST` stores the mode with the session and re-resolves the pin: the current
values of the prompt-shaping settings are frozen with it (see
`src/chat_mode.py`). Same conventions as `routes/behavior_mode_routes.py`:
flat `{"error", "error_class"}` bodies, and a session that is not the
caller's answers exactly like one that does not exist (404).

The router is built per call and included by `setup_behavior_mode_routes`, so
it is registered wherever that one already is.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from core.session_manager import SessionManager
from routes.session_routes import _verify_session_owner
from src import chat_mode

logger = logging.getLogger(__name__)


class SetChatModeRequest(BaseModel):
    mode: str


def _error(status: int, message: str, error_class: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": message, "error_class": error_class})


def build_chat_mode_router(session_manager: SessionManager) -> APIRouter:
    router = APIRouter(tags=["chat-mode"])

    @router.get("/api/sessions/{session_id}/mode")
    async def get_chat_mode(request: Request, session_id: str) -> Dict[str, Any]:
        _verify_session_owner(request, session_id, session_manager)
        return chat_mode.describe(session_id)

    @router.post("/api/sessions/{session_id}/mode")
    async def set_chat_mode(request: Request, session_id: str, body: SetChatModeRequest):
        _verify_session_owner(request, session_id, session_manager)
        if chat_mode.normalize(body.mode) is None:
            return _error(400, "mode must be 'lean' or 'normal'.", "chat_mode.invalid")
        view = chat_mode.set_mode(session_id, body.mode)
        if not view.get("persisted"):
            return _error(404, "No such session.", "chat_mode.not_found")
        return chat_mode.describe(session_id)

    return router