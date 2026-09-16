"""Per-channel credentials for Reach (`reach_<channel>_token` settings).

Values are read through `src.secret_storage.decrypt`, which is a no-op on a
plaintext value, so an operator who pastes a token unencrypted still works,
and an admin UI that stores it `enc:`-prefixed (the same convention
`oidc_client_secret`/`external_runtimes_herdr.token` use) is transparently
decrypted here. Never logged, never included in a `ReachResult` or in the
doctor output -- callers only ever see whether a token is present, not its
value.
"""
from __future__ import annotations

import logging

from src import secret_storage
from src.settings import get_setting

logger = logging.getLogger(__name__)


def get_token(channel: str) -> str:
    """Return the decrypted token/cookie configured for `channel`, or ''."""
    raw = get_setting(f"reach_{channel}_token", "") or ""
    if not raw:
        return ""
    try:
        return secret_storage.decrypt(raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning("reach.credentials: failed to decrypt reach_%s_token: %s", channel, exc)
        return ""


def has_token(channel: str) -> bool:
    return bool(get_token(channel))


def redact(value: str) -> str:
    """Never used to build output today (tokens never enter results), kept
    as the single place a future caller would redact through rather than
    hand-rolling `value[:4] + "..."` at each call site."""
    if not value:
        return ""
    if len(value) <= 6:
        return "***"
    return value[:3] + "…" + value[-2:]
