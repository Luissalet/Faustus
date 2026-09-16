"""API Token management routes — /api/tokens/*."""

import secrets
import uuid

import bcrypt
from fastapi import APIRouter, HTTPException, Request, Form

from core.database import get_db_session, ApiToken
from core.middleware import require_admin
from src.auth_helpers import get_current_user
from src import service_identity

MAX_NAME_LEN = 100
DEFAULT_SCOPES = "chat"
ALLOWED_SCOPES = {
    "chat",
    # Drive a chat session end to end from outside the app: create sessions,
    # stream turns, answer questions and in-turn tool approvals, stop,
    # resume, read history/artifacts. Everything it opens is attributed to
    # and scoped to the token's own owner (core/authz.py, docs/api/sdk_surface.md).
    "sessions",
    "todos:read",
    "todos:write",
    "documents:read",
    "documents:write",
    "email:read",
    "email:draft",
    "email:send",
    "calendar:read",
    "calendar:write",
    "memory:read",
    "memory:write",
    "cookbook:read",
    "cookbook:launch",
    # Dispatch local workers from outside the app (Fable / Claude Desktop /
    # a script): POST /api/dispatch and read the compact results.
    "agents:dispatch",
}
TOKEN_PROFILES = {
    "chat": ["chat"],
    # A paired external client that drives the full session lifecycle
    # (create session -> stream a turn over SSE -> in-turn approval ->
    # stop/resume -> read artifacts). See docs/api/sdk_surface.md.
    "sdk": ["chat", "sessions"],
    "codex_todos": ["todos:read", "todos:write"],
    "codex_documents": ["documents:read", "documents:write"],
    "codex_email_drafts": ["email:read", "email:draft", "documents:read", "documents:write"],
    "fable_workers": ["agents:dispatch"],
}


def _normalize_scopes(scopes: str | list[str] | None = None, profile: str | None = None) -> list[str]:
    profile = profile if isinstance(profile, str) else None
    profile_key = (profile or "").strip()
    if profile_key:
        if profile_key not in TOKEN_PROFILES:
            raise HTTPException(400, "Unknown token profile")
        requested = list(TOKEN_PROFILES[profile_key])
    elif isinstance(scopes, list):
        requested = [str(s).strip() for s in scopes if str(s).strip()]
    elif isinstance(scopes, str) and scopes:
        requested = [s.strip() for s in scopes.replace(" ", ",").split(",") if s.strip()]
    else:
        requested = [DEFAULT_SCOPES]

    normalized = []
    for scope in requested:
        if scope not in ALLOWED_SCOPES:
            raise HTTPException(400, f"Unknown token scope: {scope}")
        if scope not in normalized:
            normalized.append(scope)

    def ensure_before(write_scope: str, read_scope: str):
        if write_scope not in normalized or read_scope in normalized:
            return
        idx = normalized.index(write_scope)
        normalized.insert(idx, read_scope)

    ensure_before("todos:write", "todos:read")
    ensure_before("documents:write", "documents:read")
    ensure_before("calendar:write", "calendar:read")
    ensure_before("memory:write", "memory:read")
    ensure_before("email:draft", "email:read")
    ensure_before("cookbook:launch", "cookbook:read")

    return normalized or [DEFAULT_SCOPES]


def setup_api_token_routes() -> APIRouter:
    router = APIRouter(prefix="/api", tags=["api_tokens"])

    @router.get("/tokens")
    def list_tokens(request: Request):
        require_admin(request)
        with get_db_session() as db:
            tokens = db.query(ApiToken).all()
            return [
                {
                    "id": t.id,
                    "name": t.name,
                    "owner": getattr(t, "owner", None),
                    "token_prefix": t.token_prefix,
                    "scopes": [s.strip() for s in (getattr(t, "scopes", "") or DEFAULT_SCOPES).split(",") if s.strip()],
                    "is_active": t.is_active,
                    "last_used_at": t.last_used_at.isoformat() if t.last_used_at else None,
                    "created_at": t.created_at.isoformat() if t.created_at else None,
                }
                for t in tokens
            ]

    def _invalidate_cache(request: Request):
        """Tell the auth middleware its cached token map is stale."""
        try:
            invalidator = getattr(request.app.state, "invalidate_token_cache", None)
            if invalidator:
                invalidator()
        except Exception:
            pass

    @router.get("/tokens/profiles")
    def token_profiles(request: Request):
        require_admin(request)
        return {
            "profiles": TOKEN_PROFILES,
            "allowed_scopes": sorted(ALLOWED_SCOPES),
        }

    @router.post("/tokens")
    def create_token(
        request: Request,
        name: str = Form(""),
        scopes: str = Form(None),
        profile: str = Form(None),
        # A23: an unattended/machine credential. `subject` names the job or
        # service using this token (distinct from `owner`, the admin who
        # minted it); when set, this token is additionally registered as a
        # service identity (src/service_identity.py) with `scopes` REQUIRED
        # explicitly (no "chat" default — see `_normalize_scopes` below) and
        # an optional absolute expiry. Plain human/integration tokens (no
        # `subject`) are unaffected and behave exactly as before.
        subject: str = Form(None),
        expires_in_seconds: int = Form(None),
    ):
        require_admin(request)
        name = name.strip()[:MAX_NAME_LEN]
        if not name:
            raise HTTPException(400, "Token name is required")
        owner = get_current_user(request)
        subject = (subject.strip()[:MAX_NAME_LEN] or None) if isinstance(subject, str) else None
        if subject and not (isinstance(scopes, (str, list)) and scopes) and not (isinstance(profile, str) and profile):
            raise HTTPException(400, "A service identity token requires explicit scopes or a profile")
        scope_list = _normalize_scopes(scopes, profile)
        scopes_value = ",".join(scope_list)

        raw_token = "ody_" + secrets.token_urlsafe(32)
        token_hash = bcrypt.hashpw(raw_token.encode(), bcrypt.gensalt()).decode()
        token_id = str(uuid.uuid4())[:8]

        with get_db_session() as db:
            db.add(ApiToken(
                id=token_id,
                owner=owner,
                name=name,
                token_hash=token_hash,
                token_prefix=raw_token[:8],
                scopes=scopes_value,
                is_active=True,
            ))
        _invalidate_cache(request)

        expires_at = None
        if subject:
            if isinstance(expires_in_seconds, int) and expires_in_seconds > 0:
                import time as _time
                expires_at = _time.time() + int(expires_in_seconds)
            service_identity.register(token_id, subject=subject, scopes=scope_list, expires_at=expires_at)

        response = {
            "id": token_id,
            "name": name,
            "owner": owner,
            "token": raw_token,
            "token_prefix": raw_token[:8],
            "scopes": scope_list,
        }
        if subject:
            response["subject"] = subject
            response["expires_at"] = expires_at
        return response

    @router.post("/tokens/{token_id}/revoke")
    def revoke_token(request: Request, token_id: str):
        """A23: explicit revocation, distinct from DELETE — deactivates the
        `ApiToken` row (same immediate effect DELETE already had on the
        bearer-auth cache) AND, when this token has a service-identity
        record, stamps its `revoked_at` there too, so both
        `service_identity.is_revoked()` and the app's own bearer-token check
        agree the instant this call returns."""
        require_admin(request)
        current_user = get_current_user(request)
        with get_db_session() as db:
            token = db.query(ApiToken).filter(ApiToken.id == token_id).first()
            if not token:
                raise HTTPException(404, "Token not found")
            if current_user and token.owner != current_user:
                raise HTTPException(403, "Not your token")
            token.is_active = False
            db.add(token)
        service_identity.revoke(token_id)
        _invalidate_cache(request)
        return {"status": "revoked"}

    @router.patch("/tokens/{token_id}")
    async def update_token(request: Request, token_id: str):
        require_admin(request)
        current_user = get_current_user(request)
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        with get_db_session() as db:
            token = db.query(ApiToken).filter(ApiToken.id == token_id).first()
            if not token:
                raise HTTPException(404, "Token not found")
            if current_user and token.owner != current_user:
                raise HTTPException(403, "Not your token")
            if isinstance(payload.get("name"), str) and payload["name"].strip():
                token.name = payload["name"].strip()[:MAX_NAME_LEN]
            # Only touch scopes when the caller actually sent them. A partial
            # update such as a rename ({"name": ...} with no "scopes" key) must
            # not silently reset the token to the default scope — that dropped
            # every previously granted scope.
            if "scopes" in payload:
                token.scopes = ",".join(_normalize_scopes(payload.get("scopes")))
            db.add(token)
            current_scopes = [
                s.strip()
                for s in (getattr(token, "scopes", "") or DEFAULT_SCOPES).split(",")
                if s.strip()
            ]
            response = {
                "id": token_id,
                "name": getattr(token, "name", ""),
                "owner": getattr(token, "owner", None),
                "token_prefix": getattr(token, "token_prefix", ""),
                "scopes": current_scopes,
            }
        _invalidate_cache(request)
        return response

    @router.delete("/tokens/{token_id}")
    def delete_token(request: Request, token_id: str):
        require_admin(request)
        current_user = get_current_user(request)
        with get_db_session() as db:
            token = db.query(ApiToken).filter(ApiToken.id == token_id).first()
            if not token:
                raise HTTPException(404, "Token not found")
            if current_user and token.owner != current_user:
                raise HTTPException(403, "Not your token")
            db.delete(token)
        service_identity.revoke(token_id)
        _invalidate_cache(request)
        return {"status": "deleted"}

    return router
