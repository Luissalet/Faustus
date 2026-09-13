"""routes/google_oauth_routes.py — G3.2: the backend for Settings ›
Integrations › Google's setup wizard (`GoogleOAuthSetup.tsx`, G3.3).

Lets an admin configure the Google OAuth client Faustus's Calendar and
Gmail OAuth flows share — paste the `client_id`/`client_secret` Google Cloud
Console issues, or drop the `client_secret_*.json` it offers to download
instead — from inside the app, with no `.env` edit and no restart. Storage
and resolution live in `src/google_oauth_client.py` (G3.1); this router is
just its HTTP surface.

Auth: `GET` is `require_admin` (read-only, no secret in the response —
same level `routes/connector_routes.py`'s read routes use). `PUT`/`DELETE`/
`check` are `require_human` — configuring the OAuth client is the same kind
of decision as approving a launch profile or registering an executable
(`routes/connector_routes.py`'s own `require_human` routes): a person types
in credentials from Google Cloud Console, not something an agent tool call
should be able to do on its own.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from core.middleware import require_admin, require_human
from src import google_oauth_client

logger = logging.getLogger(__name__)

CONSOLE_URL = "https://console.cloud.google.com/apis/credentials"

# Fixed step keys — the Studio wizard supplies the actual (translated) copy
# per key; this list is only the order and the identity of each step.
STEPS = ["project", "enable_calendar_api", "consent_screen", "test_user", "credentials", "paste"]


def _client_id_hint(client_id: str) -> str:
    """A display-safe form of `client_id` for the "Configured" status line.

    `client_id` is not itself a secret — Google's own docs say it can be
    embedded in client-side code — but this field is named "hint" and a GET
    route is read by more than the one admin who set it up, so it never
    echoes the value whole: first 6 and last 20 characters, matching the
    shape Google's own client IDs have (`<project-number>-<hash>.apps.
    googleusercontent.com`)."""
    if len(client_id) <= 30:
        return client_id
    return f"{client_id[:6]}…{client_id[-20:]}"


def _status(request: Request, warnings: list[str] | None = None) -> dict:
    client = google_oauth_client.get_client()
    uris = google_oauth_client.redirect_uris(request)
    body: dict = {
        "configured": bool(client["client_id"] and client["client_secret"]),
        "source": client["source"],
        "client_id_hint": _client_id_hint(client["client_id"]) if client["client_id"] else None,
        "redirect_uris": {"calendar": uris["calendar"], "email": uris["email"]},
        "origin": uris["origin"],
        "console_url": CONSOLE_URL,
        "steps": list(STEPS),
    }
    if warnings:
        body["warnings"] = warnings
    return body


def setup_google_oauth_routes() -> APIRouter:
    router = APIRouter(prefix="/api/google", tags=["google-oauth"])

    @router.get("/oauth-client")
    async def get_oauth_client(request: Request):
        require_admin(request)
        return _status(request)

    @router.put("/oauth-client")
    async def put_oauth_client(request: Request):
        require_human(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}

        warnings: list[str] = []
        raw_json = body.get("client_secret_json")
        if raw_json:
            try:
                parsed = google_oauth_client.parse_client_secret_json(str(raw_json))
            except ValueError as exc:
                raise HTTPException(400, str(exc))
            client_id = parsed["client_id"]
            client_secret = parsed["client_secret"]
            uris = google_oauth_client.redirect_uris(request)
            ours = {uris["calendar"], uris["email"]}
            in_file = set(parsed.get("redirect_uris") or [])
            # Only flags a genuine gap — a file that lists neither of ours
            # (or lists none at all, e.g. hand-trimmed before pasting) — not
            # one that simply has MORE than we need.
            missing = sorted(ours - in_file)
            if in_file and missing:
                warnings.append("Add these redirect URIs in Google Cloud: " + ", ".join(missing))
        else:
            client_id = str(body.get("client_id") or "").strip()
            client_secret = str(body.get("client_secret") or "").strip()

        if not client_id.endswith(".apps.googleusercontent.com"):
            raise HTTPException(
                400,
                "That doesn't look like a Google OAuth client ID — it should end "
                "with .apps.googleusercontent.com",
            )
        if not client_secret:
            raise HTTPException(400, "A client secret is required.")

        google_oauth_client.set_client(client_id, client_secret)
        return _status(request, warnings=warnings or None)

    @router.delete("/oauth-client")
    async def delete_oauth_client(request: Request):
        require_human(request)
        google_oauth_client.clear_client()
        return {"ok": True}

    @router.post("/oauth-client/check")
    async def check_oauth_client(request: Request):
        """A real but harmless call to Google: a token refresh with a
        refresh_token Google can never have issued. `invalid_client` means
        the id/secret pair itself is wrong; `invalid_grant` means Google
        accepted the client and rejected only the (deliberately bogus)
        token — exactly the outcome a correctly configured client gives, so
        it's success here. Never returns a secret."""
        require_human(request)
        client = google_oauth_client.get_client()
        client_id = client["client_id"] or ""
        client_secret = client["client_secret"] or ""
        if not client_id or not client_secret:
            raise HTTPException(400, google_oauth_client.NOT_CONFIGURED_MESSAGE)

        import httpx
        try:
            resp = httpx.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "refresh_token": "invalid",
                    "grant_type": "refresh_token",
                },
                timeout=10,
            )
        except Exception:
            logger.warning("Google OAuth client check request failed")
            return {"ok": False, "detail": "Could not reach Google — check the network connection."}

        try:
            payload = resp.json()
        except Exception:
            payload = {}
        error = payload.get("error") if isinstance(payload, dict) else None
        if error == "invalid_client":
            return {"ok": False, "detail": "Google rejected the client ID or secret."}
        if error == "invalid_grant":
            return {"ok": True, "detail": "Google accepted the client ID and secret."}
        logger.warning("Google OAuth client check got an unexpected response (status=%s)", resp.status_code)
        return {"ok": False, "detail": f"Unexpected response from Google (status {resp.status_code})."}

    return router
