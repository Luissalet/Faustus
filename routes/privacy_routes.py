"""routes/privacy_routes.py — GET/PUT /api/privacy/profile (SEC-04).

`src.privacy_policy` is a real, enforced gate (`assert_outbound`, wired into
the embedding lane, ChromaDB, the compaction summarizer and the reranker —
see that module's docstring for the full list and what still needs wiring
outside this route's scope) — but nothing let an admin SEE or CHANGE which
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
            # for this key — the profile value itself is already validated
            # against `privacy_policy.PROFILES` above.
            raise HTTPException(status_code=400, detail=str(e))
        return {"profile": payload.profile, "profiles": list(privacy_policy.PROFILES)}

    return router
