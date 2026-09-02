"""Agent settings schema API.

GET /api/agent/settings/schema → {"groups": [...], "defaults": {...}} (admin only)

The declarative description of every agent_* / browser_* / desktop_* setting
(src/agent_settings_schema.py). Settings → Agent Tools renders the form from
it (static/js/agentSettings.js); the values themselves keep flowing through
GET / POST /api/auth/settings, so this route only ever describes keys.
"""
from __future__ import annotations

from fastapi import APIRouter, Request

from core.middleware import require_admin
from src.agent_settings_schema import build_schema


def setup_agent_settings_routes() -> APIRouter:
    router = APIRouter(prefix="/api/agent", tags=["agent"])

    @router.get("/settings/schema")
    def get_agent_settings_schema(request: Request):
        # Same gate as POST /api/auth/settings: the schema names every knob and
        # its default, which is admin-facing information.
        require_admin(request)
        return build_schema()

    return router
