"""Google Calendar account storage and OAuth token lifecycle.

Mirrors ``src/caldav_sync.py``'s ``_load_caldav_accounts`` persistence
pattern: accounts live in the owner's prefs (``routes.prefs_routes``) under
the ``google_calendar_accounts`` key, as a list of dicts:

    {"id": "<uuid>", "label": "Google · luis@gmail.com", "email": "luis@gmail.com",
     "access_token": "<enc>", "refresh_token": "<enc>", "token_expiry": "<unix>",
     "status": "ok" | "needs_reauth", "sync_tokens": {"<calendar_id>": "<syncToken>"},
     "selected_calendars": null | ["<google calendar id>", ...],
     "created_at": <epoch float>, "last_sync_at": <epoch float | None>}

``access_token`` / ``refresh_token`` are always ``src.secret_storage``-encrypted
at rest, never returned to a caller unless it explicitly asks for the raw
(internal, ``public=False``) row shape, and never logged.

This module makes its own outbound HTTP calls with the bare ``httpx.post``
function (not a client object), mirroring ``routes/email_helpers.py``'s
``_refresh_google_token`` — tests inject a fake Google by monkeypatching
``httpx.post`` (see ``routes.email_helpers`` / ``tests/test_email_oauth.py``
for the established pattern this follows).
"""

import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)

_PUBLIC_FIELDS = ("id", "label", "email", "status", "last_sync_at", "selected_calendars")


def client_configured() -> bool:
    """Whether a Google OAuth client (shared with the email OAuth flow) is set."""
    return bool(os.environ.get("GOOGLE_OAUTH_CLIENT_ID")) and bool(
        os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
    )


def _load_accounts_raw(owner: str) -> list:
    from routes.prefs_routes import _load_for_user

    prefs = _load_for_user(owner) or {}
    accounts = prefs.get("google_calendar_accounts")
    return list(accounts) if isinstance(accounts, list) else []


def _save_accounts_raw(owner: str, accounts: list) -> None:
    from routes.prefs_routes import _load_for_user, _save_for_user

    prefs = _load_for_user(owner) or {}
    prefs["google_calendar_accounts"] = accounts
    _save_for_user(owner, prefs)


def _to_public(acc: dict) -> dict:
    return {k: acc.get(k) for k in _PUBLIC_FIELDS}


def list_accounts(owner: str, *, public: bool = True) -> list:
    """All configured Google Calendar accounts for *owner*.

    ``public=True`` (default) strips ``access_token``/``refresh_token`` —
    safe to return straight from an API route. ``public=False`` is for
    internal callers (token refresh, sync) that need the encrypted tokens.
    """
    accounts = _load_accounts_raw(owner)
    if public:
        return [_to_public(a) for a in accounts]
    return [dict(a) for a in accounts]


def get_account(owner: str, account_id: str, *, public: bool = True) -> Optional[dict]:
    for a in _load_accounts_raw(owner):
        if a.get("id") == account_id:
            return _to_public(a) if public else dict(a)
    return None


def upsert_account(owner: str, account: dict) -> None:
    """Insert or replace *account* (matched by ``id``)."""
    accounts = _load_accounts_raw(owner)
    account_id = account.get("id")
    idx = next((i for i, a in enumerate(accounts) if a.get("id") == account_id), None)
    if idx is None:
        accounts.append(account)
    else:
        accounts[idx] = account
    _save_accounts_raw(owner, accounts)


def _revoke_best_effort(acc: dict) -> None:
    """Best-effort token revocation at Google. Never raises."""
    from src.secret_storage import decrypt as _dec

    token = _dec(acc.get("refresh_token") or "") or _dec(acc.get("access_token") or "")
    if not token:
        return
    try:
        import httpx

        httpx.post("https://oauth2.googleapis.com/revoke", data={"token": token}, timeout=10)
    except Exception:
        logger.warning(
            "Google Calendar token revoke failed for account %s..", str(acc.get("id") or "")[:8]
        )


def delete_account(owner: str, account_id: str) -> bool:
    """Remove an account, revoking its token at Google best-effort.

    Returns False if no such account exists (caller answers 404).
    """
    accounts = _load_accounts_raw(owner)
    idx = next((i for i, a in enumerate(accounts) if a.get("id") == account_id), None)
    if idx is None:
        return False
    _revoke_best_effort(accounts[idx])
    del accounts[idx]
    _save_accounts_raw(owner, accounts)
    return True


def _mark_needs_reauth(owner: str, account: dict) -> None:
    account = dict(account)
    account["status"] = "needs_reauth"
    upsert_account(owner, account)


def access_token_for(owner: str, account_id: str, *, force_refresh: bool = False) -> Optional[str]:
    """A currently-valid Google access token for *account_id*, refreshing it
    when it expires within 60s (or when ``force_refresh`` is set — used by
    callers retrying once after an unexpected 401).

    Returns None when the account doesn't exist, the OAuth client isn't
    configured, or the refresh fails. A refresh failure whose Google error is
    specifically ``invalid_grant`` (the refresh token was revoked/expired)
    marks the account ``status="needs_reauth"`` so the UI can prompt a
    reconnect; other failures (network blip, 5xx) leave status alone so the
    next sync just retries.
    """
    from src.secret_storage import decrypt as _dec, encrypt as _enc

    account = get_account(owner, account_id, public=False)
    if not account:
        return None

    if not force_refresh:
        access_token = _dec(account.get("access_token") or "")
        expiry_str = str(account.get("token_expiry") or "")
        if access_token and expiry_str:
            try:
                if int(float(expiry_str)) - 60 > time.time():
                    return access_token
            except (TypeError, ValueError):
                pass

    if not client_configured():
        return None

    refresh_token = _dec(account.get("refresh_token") or "")
    if not refresh_token:
        return None

    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
    try:
        import httpx

        resp = httpx.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=10,
        )
    except Exception:
        logger.warning(
            "Google Calendar token refresh request failed for account %s..", account_id[:8]
        )
        return None

    if resp.status_code >= 400:
        try:
            body = resp.json()
        except Exception:
            body = {}
        if body.get("error") == "invalid_grant":
            _mark_needs_reauth(owner, account)
            logger.warning(
                "Google Calendar refresh token invalid for account %s.. — needs reauth",
                account_id[:8],
            )
        else:
            logger.warning(
                "Google Calendar token refresh failed for account %s.. (status=%s)",
                account_id[:8], resp.status_code,
            )
        return None

    try:
        data = resp.json()
        access_token = data["access_token"]
    except Exception:
        logger.warning("Google Calendar token refresh returned an unexpected body")
        return None

    expiry = int(time.time()) + int(data.get("expires_in", 3600))
    account = dict(account)
    account["access_token"] = _enc(access_token)
    account["token_expiry"] = str(expiry)
    account["status"] = "ok"
    upsert_account(owner, account)
    return access_token
