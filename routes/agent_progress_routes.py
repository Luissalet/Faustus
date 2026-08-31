"""Agent progress API — the todowrite list the agent maintains per chat.

GET /api/agent/progress/{session_id} → {"todos": [...], "updated_at": float|None}

The agent tool `todowrite` (src/agent_tools/coding_tools.py) persists the list
under data/agent_todos/<session>.json; the chat UI streams live updates via the
`progress_update` SSE event and uses this endpoint to restore the panel when a
chat is reopened.
"""
from __future__ import annotations

import json
import os
import re

from fastapi import APIRouter, HTTPException, Request

from src.auth_helpers import require_user
from src.constants import DATA_DIR
# The SAME helper every sibling per-session route uses (routes/history/
# history_routes.py, routes/chat_routes.py): authenticated callers must match
# the session's stored owner, and with AUTH_ENABLED=false it degrades to "the
# session must exist". Duplicating that logic here is how the two drift apart,
# so it is imported, not re-implemented.
from routes.session_routes import _verify_session_owner

_TODO_DIR = os.path.join(DATA_DIR, "agent_todos")


def _safe_session_id(value: str) -> str:
    value = value or "current"
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)[:120] or "current"


def setup_agent_progress_routes() -> APIRouter:
    router = APIRouter(prefix="/api/agent", tags=["agent"])

    @router.get("/progress/{session_id}")
    def get_progress(request: Request, session_id: str):
        require_user(request)
        # Reading a chat's to-do list is reading that chat: same gate as
        # GET /api/session/{id}/versions and /api/chat/resume. A session that
        # does not exist 404s, which is what the panel already handles (the UI
        # bails on !r.ok) and is strictly better than serving whatever file
        # happens to sit at that name.
        _verify_session_owner(request, session_id)
        path = os.path.join(_TODO_DIR, f"{_safe_session_id(session_id)}.json")
        if not os.path.isfile(path):
            return {"todos": [], "updated_at": None}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            raise HTTPException(500, "progress file unreadable")
        todos = data.get("todos") if isinstance(data, dict) else None
        return {"todos": todos if isinstance(todos, list) else [], "updated_at": os.path.getmtime(path)}

    @router.delete("/progress/{session_id}")
    def clear_progress(request: Request, session_id: str):
        require_user(request)
        # Same ownership gate as the GET, with one deliberate exception for
        # ORPHAN PROGRESS. _verify_session_owner answers 404 for two different
        # things: "no such session anywhere" and "it is someone else's". Those
        # must not be treated alike — but they are distinguishable without
        # re-implementing the check: case two can only happen when there IS an
        # authenticated user to compare against. So when no user is present
        # (AUTH_ENABLED=false, the normal local setup) a 404 can only mean the
        # session is gone, and deleting its leftover to-do file is exactly the
        # cleanup this endpoint is for — refusing would strand the file forever,
        # since a deleted session never passes the check again. With auth on,
        # the 404 stands and another user's file is never touched.
        try:
            _verify_session_owner(request, session_id)
        except HTTPException as exc:
            from src.auth_helpers import effective_user
            if exc.status_code != 404 or effective_user(request):
                raise
        path = os.path.join(_TODO_DIR, f"{_safe_session_id(session_id)}.json")
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError:
            raise HTTPException(500, "could not remove progress file")
        return {"ok": True}

    return router
