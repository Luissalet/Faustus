"""Secret-scrubbing for settings exposed to non-admin / unauthenticated callers.

Deliberately dependency-light (stdlib only) and separate from
``routes/auth_routes.py`` so it can be imported and unit-tested without dragging
in the FastAPI app / auth / database import chain.

``/api/auth/settings`` is auth-exempt — the frontend (and the pre-login page)
read it for keybinds + TTS prefs, so non-admin and unauthenticated callers get a
*scrubbed* copy. Secrets (provider API keys, IMAP/SMTP passwords, OAuth tokens)
must NOT leak to them — load-bearing when the app is reachable over a Cloudflare
tunnel / reverse proxy. Scrubbing is deep (recurses nested dicts/lists) and keyed
on secret-shaped names.
"""

import re

_SECRET_KEY_PATTERNS = (
    "_api_key", "_apikey", "_password", "_passwd", "_pass", "_pwd",
    "_secret", "_client_secret", "_token", "_access_token", "_refresh_token",
    "_credential", "_credentials", "_key",
)
_SECRET_KEY_ALLOW = ("google_pse_cx",)  # public identifiers, not secrets
_SENSITIVE_KEY_EXACT = (
    # A stable global integration id is a capability handle for routes that can
    # trigger outbound webhook sends; do not expose it to non-admin settings
    # callers even though it is not secret-shaped.
    "reminder_webhook_integration_id",
)


def _canonical_key_name(name: str) -> str:
    """Normalize common JS-style key names so secret matching is style-agnostic."""
    n = (name or "").replace("-", "_")
    n = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", n)
    n = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", n)
    return n.lower()


def is_secret_key(name: str) -> bool:
    n = _canonical_key_name(name)
    if n in _SECRET_KEY_ALLOW:
        return False
    if n in _SENSITIVE_KEY_EXACT:
        return True
    return any(n.endswith(p) or n == p.lstrip("_") for p in _SECRET_KEY_PATTERNS)


def _scrub_value(key, value):
    """Mask secret-shaped leaves, recursing into nested dicts/lists so a secret
    stored under a non-secret parent key (e.g.
    ``{"email_account": {"smtp_password": "..."}}``) is still blanked. Only
    non-empty *string* values are blanked; presence is preserved."""
    if isinstance(value, dict):
        return {
            k: ("" if (is_secret_key(k) and isinstance(v, str) and v)
                else _scrub_value(k, v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_scrub_value(key, item) for item in value]
    if is_secret_key(key) and isinstance(value, str) and value:
        return ""
    return value


def scrub_settings(settings: dict) -> dict:
    """Return a copy of ``settings`` with secret-shaped values masked (deep)."""
    if not isinstance(settings, dict):
        return {}
    return {k: _scrub_value(k, v) for k, v in (settings or {}).items()}


# ---- Write-only credentials for admins ------------------------------------
#
# Admins used to read every credential back in clear. A key the server holds
# is only ever needed by the server, so the admin read now shows a mask that
# keeps the last four characters (enough to tell two keys apart) and the save
# path swaps a mask sent back for the stored value. Posting a mask can
# therefore never overwrite a real key, and the key never travels to the
# browser again after it was typed.

MASK = "••••"


def _is_credential_key(name: str) -> bool:
    """Secret-shaped names only: exact sensitive ids (a webhook integration
    id picked from a list) stay readable to admins, who need the real value."""
    n = _canonical_key_name(name)
    if n in _SENSITIVE_KEY_EXACT:
        return False
    return is_secret_key(name)


def mask_value(secret: str) -> str:
    """``••••`` plus the last four characters when the key is long enough for
    that tail not to give most of it away."""
    if not isinstance(secret, str) or not secret:
        return secret
    return MASK + secret[-4:] if len(secret) >= 12 else MASK


def _mask_value(key, value):
    if isinstance(value, dict):
        return {k: _mask_value(k, v) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask_value(key, item) for item in value]
    if _is_credential_key(key) and isinstance(value, str) and value:
        return mask_value(value)
    return value


def mask_secrets(settings: dict) -> dict:
    """The admin view: credentials shown as a mask, everything else as is."""
    if not isinstance(settings, dict):
        return {}
    return {k: _mask_value(k, v) for k, v in settings.items()}


def restore_masked(key, new, old):
    """Undo the mask on the way back in (deep).

    A credential whose incoming value carries the mask marker keeps the stored
    value: either the form sent the mask straight back, or someone edited
    around it — neither is a key worth storing. An empty string still clears
    the key, and any other string replaces it."""
    if isinstance(new, dict):
        base = old if isinstance(old, dict) else {}
        return {k: restore_masked(k, v, base.get(k)) for k, v in new.items()}
    if isinstance(new, list):
        base = old if isinstance(old, list) else []
        return [restore_masked(key, item, base[i] if i < len(base) else None)
                for i, item in enumerate(new)]
    if _is_credential_key(key) and isinstance(new, str) and MASK in new:
        return old if isinstance(old, str) else ""
    return new
