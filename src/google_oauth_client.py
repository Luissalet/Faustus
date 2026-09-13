"""src/google_oauth_client.py — G3.1: single source of truth for Google's
OAuth client (the `client_id`/`client_secret` pair Google Cloud issues for
one project — shared by the Calendar and Gmail OAuth flows, one scope added
per feature, never a second client).

Resolution order (`get_client`): this module's own store
(`DATA_DIR/google_oauth_client.json`, written by Settings › Integrations ›
Google — G3.2/G3.3) first, then the `GOOGLE_OAUTH_CLIENT_ID`/
`GOOGLE_OAUTH_CLIENT_SECRET` environment variables, unchanged, so Docker/CI
deployments that already set them keep working with no migration. Every
caller that used to read those two env vars directly —
`src/google_calendar_accounts.py`, `routes/calendar_routes.py`,
`routes/email_routes.py`, `routes/email_helpers.py` — now calls
`get_client()` instead, so saving a client from the UI takes effect on the
next request, no restart.

The stored `client_secret` is always `src.secret_storage`-encrypted at rest;
`get_client()` decrypts it in memory and this module never logs either
value. `redirect_uris(request)` centralizes the scheme+host inference (with
`X-Forwarded-Proto`/`X-Forwarded-Host` support behind a reverse proxy) that
`routes/calendar_routes.py` and `routes/email_routes.py` used to each carry
their own copy of.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional, TypedDict
from urllib.parse import urlparse, urlunparse

from core.atomic_io import atomic_write_json
from core.platform_compat import safe_chmod
from src.constants import DATA_DIR as _DEFAULT_DATA_DIR
from src.secret_storage import decrypt as _dec, encrypt as _enc

logger = logging.getLogger(__name__)

# Module-level, like src/launch_profiles.py's own `DATA_DIR` — tests point
# this at a disposable tmp_path instead of the real data dir.
DATA_DIR = _DEFAULT_DATA_DIR

NOT_CONFIGURED_MESSAGE = "Google OAuth client not configured — Settings › Integrations › Google"


class ClientInfo(TypedDict):
    client_id: Optional[str]
    client_secret: Optional[str]
    source: Optional[str]  # "stored" | "env" | None


def _store_path() -> str:
    return os.path.join(DATA_DIR, "google_oauth_client.json")


def _load_raw() -> Optional[Dict[str, Any]]:
    path = _store_path()
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("Failed to load the stored Google OAuth client: %s", exc)
        return None
    if not isinstance(raw, dict):
        return None
    return raw


def get_client() -> ClientInfo:
    """The Google OAuth client to use right now: the stored one if present
    and complete, else the environment variables, else nothing configured.
    Never logs `client_id` or `client_secret`."""
    raw = _load_raw()
    if raw and raw.get("client_id") and raw.get("client_secret"):
        secret = _dec(str(raw["client_secret"]))
        if secret:
            return {"client_id": str(raw["client_id"]), "client_secret": secret, "source": "stored"}
        logger.error("Stored Google OAuth client secret failed to decrypt — falling back to env")
    env_id = (os.environ.get("GOOGLE_OAUTH_CLIENT_ID") or "").strip()
    env_secret = (os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET") or "").strip()
    if env_id and env_secret:
        return {"client_id": env_id, "client_secret": env_secret, "source": "env"}
    return {"client_id": None, "client_secret": None, "source": None}


def configured() -> bool:
    client = get_client()
    return bool(client["client_id"]) and bool(client["client_secret"])


def set_client(client_id: str, client_secret: str) -> None:
    """Persist the client, overwriting any previously stored one. The
    secret is encrypted before it ever touches disk."""
    path = _store_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    data = {"client_id": (client_id or "").strip(), "client_secret": _enc((client_secret or "").strip())}
    atomic_write_json(path, data, indent=2, private=True)
    safe_chmod(path, 0o600)


def clear_client() -> None:
    """Remove the stored client. Never touches the environment variables —
    a Docker/CI deploy that sets them keeps working exactly as before."""
    try:
        os.remove(_store_path())
    except FileNotFoundError:
        pass


def parse_client_secret_json(text: str) -> Dict[str, Any]:
    """Parse the `client_secret_*.json` Google Cloud Console downloads for
    an OAuth client. Accepts both shapes Google issues — `{"web": {...}}`
    (Web application clients, what this flow needs) and `{"installed":
    {...}}` (Desktop app clients, in case someone pastes the wrong one so
    the message can say so). Returns `{"client_id", "client_secret",
    "redirect_uris"}`. Raises `ValueError` with a message safe to show the
    user; never logs `text`."""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("That doesn't look like valid JSON.") from exc
    if not isinstance(data, dict):
        raise ValueError("That doesn't look like a Google OAuth client file.")
    block = data.get("web") or data.get("installed")
    if not isinstance(block, dict):
        raise ValueError(
            'That JSON file doesn\'t look like a Google OAuth client — expected a '
            '"web" or "installed" section (the file Google Cloud Console downloads '
            "for an OAuth client)."
        )
    client_id = str(block.get("client_id") or "").strip()
    client_secret = str(block.get("client_secret") or "").strip()
    if not client_id or not client_secret:
        raise ValueError("That JSON file is missing client_id or client_secret.")
    raw_uris = block.get("redirect_uris")
    redirect_uris_in_file: List[str] = [str(u) for u in raw_uris] if isinstance(raw_uris, list) else []
    return {"client_id": client_id, "client_secret": client_secret, "redirect_uris": redirect_uris_in_file}


def _first_header(request: Any, name: str) -> str:
    value = request.headers.get(name) or ""
    return value.split(",")[0].strip()


def _with_path(url: str, path: str) -> str:
    parsed = urlparse(url)
    return urlunparse(parsed._replace(path=path))


def redirect_uris(request: Any) -> Dict[str, str]:
    """`{"calendar": url, "email": url, "origin": "scheme://host"}` for the
    URL a browser is actually reaching Faustus at right now.

    Origin resolution: `X-Forwarded-Proto`/`X-Forwarded-Host` win when
    present (reverse proxy), else the request's own scheme and `Host`
    header — same inference `routes/calendar_routes.py`'s
    `_google_calendar_redirect_uri` and `routes/email_routes.py`'s inline
    fallback used to each compute separately.

    `GOOGLE_OAUTH_REDIRECT_URI` (email) and `GOOGLE_CALENDAR_OAUTH_REDIRECT_URI`
    (calendar) still override individually when set, used verbatim. With
    only the email one set, the calendar URI is derived by swapping its
    *path* (never a string-replace of "/email/" for "/calendar/", since
    nothing guarantees that substring is present)."""
    scheme = _first_header(request, "x-forwarded-proto") or getattr(request.url, "scheme", "") or "http"
    host = _first_header(request, "x-forwarded-host") or (request.headers.get("host") or "localhost:7000")
    origin = f"{scheme}://{host}"

    email_explicit = (os.environ.get("GOOGLE_OAUTH_REDIRECT_URI") or "").strip()
    email_uri = email_explicit or f"{origin}/api/email/oauth/google/callback"

    calendar_explicit = (os.environ.get("GOOGLE_CALENDAR_OAUTH_REDIRECT_URI") or "").strip()
    if calendar_explicit:
        calendar_uri = calendar_explicit
    elif email_explicit:
        calendar_uri = _with_path(email_explicit, "/api/calendar/oauth/google/callback")
    else:
        calendar_uri = f"{origin}/api/calendar/oauth/google/callback"

    return {"calendar": calendar_uri, "email": email_uri, "origin": origin}
