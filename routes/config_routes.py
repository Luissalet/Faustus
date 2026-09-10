"""Effective configuration API — /api/config/effective (src/effective_config.py).

  GET /api/config/effective?session=&project=&model=
      Every known field, resolved global -> project -> role/preset -> model ->
      turn, with its source and what every weaker level wanted instead
      (`overridden`), plus same-level `conflicts` and a stable `hash`. Secrets
      are masked (`core.log_safety.redact_secrets`) before this ever builds
      the response — never returned, not even to the owner asking.

  GET /api/config/effective/prompt?session=
      The tool-section blocks that would enter the prompt, with their origin.
      Calls the SAME assembly `src/agent_loop.py` uses for a real turn
      (`base_prompt_with_origin`); see that function's docstring, and
      `effective_config.prompt_blocks_for_session`, for the one thing it
      cannot reproduce without the turn's own message (RAG-narrowed tool
      selection).

Auth mirrors ``routes/agent_settings_routes.py``: this surface names every
`agent_*` setting's live value, which is admin-facing information the same
way the settings schema is — a masked value is still "here is exactly how
this instance is configured." ``session`` is optional; when given, the
caller must own that session (``routes/session_routes._verify_session_owner``,
the same helper every sibling per-session route uses) — otherwise a signed-in
admin can read a config that never names a session's contents.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from core.middleware import require_admin
from src import effective_config

logger = logging.getLogger(__name__)


def setup_config_routes() -> APIRouter:
    router = APIRouter(prefix="/api/config", tags=["config"])

    @router.get("/effective")
    def get_effective_config(
        request: Request, session: str = "", project: str = "", model: str = "",
    ):
        require_admin(request)
        owner = _owner_for(request)
        if session:
            _verify_session(request, session)
        try:
            cfg = effective_config.compile_effective(
                owner=owner, session_id=session or None, project_id=project or None,
                model=model or None,
            )
        except Exception as exc:  # noqa: BLE001 - never 500 an explain endpoint
            logger.warning("effective config compile failed: %s", exc, exc_info=True)
            raise HTTPException(status_code=500, detail="could not compile the effective configuration")
        return cfg.to_dict()

    @router.get("/effective/prompt")
    def get_effective_prompt(request: Request, session: str = ""):
        require_admin(request)
        owner = _owner_for(request)
        if session:
            _verify_session(request, session)
        try:
            return effective_config.prompt_blocks_for_session(owner=owner, session_id=session or None)
        except Exception as exc:  # noqa: BLE001
            logger.warning("effective prompt preview failed: %s", exc, exc_info=True)
            raise HTTPException(status_code=500, detail="could not build the prompt preview")

    return router


def _owner_for(request: Request) -> str:
    try:
        from src.auth_helpers import effective_user
        return effective_user(request) or ""
    except Exception:  # noqa: BLE001
        return ""


def _verify_session(request: Request, session_id: str) -> None:
    # Same gate every sibling per-session route uses (routes/agent_progress_routes.py,
    # routes/history/history_routes.py, routes/chat_routes.py) — imported, not
    # re-implemented, so the ownership rule cannot drift between the two.
    from routes.session_routes import _verify_session_owner
    _verify_session_owner(request, session_id)
