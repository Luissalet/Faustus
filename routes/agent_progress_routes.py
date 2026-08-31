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

_TODO_DIR = os.path.join(DATA_DIR, "agent_todos")


def _safe_session_id(value: str) -> str:
    value = value or "current"
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)[:120] or "current"


def setup_agent_progress_routes() -> APIRouter:
    router = APIRouter(prefix="/api/agent", tags=["agent"])

    @router.get("/progress/{session_id}")
    def get_progress(request: Request, session_id: str):
        require_user(request)
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
        path = os.path.join(_TODO_DIR, f"{_safe_session_id(session_id)}.json")
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError:
            raise HTTPException(500, "could not remove progress file")
        return {"ok": True}

    return router
