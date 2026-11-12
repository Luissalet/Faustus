"""routes/privacy_routes.py â€” GET/PUT /api/privacy/profile (SEC-04).

`src.privacy_policy` is a real, enforced gate (`assert_outbound`, wired into
the embedding lane, ChromaDB, the compaction summarizer and the reranker â€”
see that module's docstring for the full list and what still needs wiring
outside this route's scope) â€” but nothing let an admin SEE or CHANGE which
of the three profiles (`local_only` / `local_preferred` / `cloud_allowed`) is
active short of hand-editing `data/settings.json`. This is that surface: the
read/write endpoint a Settings-screen selector calls (Studio wiring is a
separate lot).

Deliberately admin-only and deliberately a single global setting: the
per-PROJECT override already exists (a project row's own `privacy_profile`
field, read directly by `privacy_policy.get_privacy_profile`) and is edited
through the ordinary project-update route, not duplicated here.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, field_validator

from core.middleware import require_admin
from src import privacy_policy

logger = logging.getLogger(__name__)


class PrivacyProfileUpdate(BaseModel):
    profile: str

    @field_validator("profile")
    @classmethod
    def _known_profile(cls, value: str) -> str:
        value = (value or "").strip()
        if value not in privacy_policy.PROFILES:
            raise ValueError(
                f"profile must be one of: {', '.join(privacy_policy.PROFILES)}"
            )
        return value


def setup_privacy_routes() -> APIRouter:
    router = APIRouter(prefix="/api/privacy", tags=["privacy"])

    @router.get("/report")
    def get_report(request: Request) -> Dict[str, Any]:
        require_admin(request)
        from src.data_boundary_report import report
        return report()

    @router.get("/attachment-destination")
    def attachment_destination(request: Request, endpoint_id: str = "", session_id: str = "") -> Dict[str, Any]:
        from core.database import SessionLocal, Session, ModelEndpoint
        from src.auth_helpers import require_authenticated_request, effective_user, owner_filter
        from src.data_boundary_report import destination
        from src.settings import get_user_setting
        require_authenticated_request(request)
        owner = effective_user(request) or ""
        with SessionLocal() as db:
            session = None
            if session_id:
                session = owner_filter(db.query(Session.id, Session.endpoint_url), Session, owner).filter(Session.id == session_id).first()
                if session is None:
                    raise HTTPException(404, "Session not found")
            # The explicit picker selection wins, including for a new chat.
            selected_id = endpoint_id or ("" if session else get_user_setting("default_endpoint_id", owner, ""))
            query = owner_filter(db.query(ModelEndpoint.base_url, ModelEndpoint.endpoint_kind), ModelEndpoint, owner)
            if selected_id:
                ep = query.filter(ModelEndpoint.id == selected_id).first()
                if ep is None:
                    raise HTTPException(404, "Endpoint not found")
            else:
                ep = query.filter(ModelEndpoint.base_url == session.endpoint_url).first() if session else None
            target = destination(ep.base_url, ep.endpoint_kind) if ep else destination(session.endpoint_url if session else "")
        return {**target, "warning": target["scope"] != "machine"}

    @router.get("/profile")
    def get_profile(request: Request) -> Dict[str, Any]:
        require_admin(request)
        from src import settings as settings_mod

        return {
            "profile": privacy_policy.get_privacy_profile(),
            "profiles": list(privacy_policy.PROFILES),
            "default_profile": privacy_policy.DEFAULT_PROFILE,
            # True once an admin has explicitly chosen a value, as opposed to
            # the module quietly falling back to DEFAULT_PROFILE.
            "is_explicit": settings_mod.is_setting_overridden(privacy_policy.SETTING_KEY),
        }

    @router.put("/profile")
    def set_profile(payload: PrivacyProfileUpdate, request: Request) -> Dict[str, Any]:
        require_admin(request)
        from src import settings as settings_mod

        try:
            settings_mod.update_settings({privacy_policy.SETTING_KEY: payload.profile})
        except settings_mod.SettingsError as e:
            # Only reachable if a future edit narrows DEFAULT_SETTINGS's type
            # for this key â€” the profile value itself is already validated
            # against `privacy_policy.PROFILES` above.
            raise HTTPException(status_code=400, detail=str(e))
        return {"profile": payload.profile, "profiles": list(privacy_policy.PROFILES)}

    return router
