"""src/service_identity.py — machine credentials (A23), layered on the
existing `ody_...` API tokens (`core/database.ApiToken`, `routes/
api_token_routes.py`).

An `ApiToken` row already carries everything a human integration needs
(owner, hashed secret, comma-separated `scopes`, `is_active`). A *service*
identity is the same token used by an unattended job (a scheduled task, a
webhook handler) rather than a person's browser, and it needs three things
the row alone doesn't record: an explicit machine `subject` (distinct from
the human `owner` who minted it), an optional absolute expiry, and a
`revoked_at` timestamp separate from the boolean `is_active` flip so a
revocation has a recorded moment, not just a before/after state.

Rather than adding columns to `ApiToken` (core/database.py is not this
lot's file — see `<LOTE>_wiring.md` if that ever needs to change), this
metadata lives in its own small JSON store, `data/service_identity.json`,
keyed by the token's id — the same `atomic_write_json` pattern `core/
auth.py` already uses for `auth.json`/`sessions.json`.

Revocation itself still goes through the real, already-immediate path: the
app's bearer-token authentication (`app.py`'s `AuthMiddleware`) is backed
by an in-memory prefix->row cache that gets marked dirty the instant a
token is deleted or deactivated (`routes/api_token_routes.py`'s
`_invalidate_cache`) and is refreshed from the database — synchronously,
under a lock — before the next bearer request is ever matched against it.
That is what makes the *documented* bound (`token_revocation_bound_seconds`,
default 0) true by construction: with no positive bound configured, this
module never caches a "not revoked" answer at all, so a revoke() recorded
here is visible to the very next `is_revoked()`/`authorize()` call with no
propagation delay.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from core.atomic_io import atomic_write_json
from src.constants import DATA_DIR

logger = logging.getLogger(__name__)

SERVICE_IDENTITY_FILE = os.path.join(DATA_DIR, "service_identity.json")

_lock = threading.RLock()

#: token_id -> (revoked_bool, cached_at). Only ever consulted when
#: `token_revocation_bound_seconds` > 0 — see `is_revoked`.
_revocation_cache: Dict[str, Tuple[bool, float]] = {}


def _read() -> Dict[str, Any]:
    try:
        with open(SERVICE_IDENTITY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (ValueError, OSError) as e:
        logger.warning("service_identity: failed to read %s: %s", SERVICE_IDENTITY_FILE, e)
        return {}


def _write(data: Dict[str, Any]) -> None:
    atomic_write_json(SERVICE_IDENTITY_FILE, data, indent=2, private=True)


def register(token_id: str, *, subject: str, scopes: List[str],
             expires_at: Optional[float] = None) -> None:
    """Record a newly-minted token's service-identity metadata: the machine
    `subject` it authenticates, its explicit `scopes` (no implicit default —
    a service identity always states what it may do), and an optional
    absolute-epoch `expires_at`. Idempotent — a second `register()` for the
    same `token_id` overwrites the previous record and clears its
    `revoked_at`, which is intentional: re-registering is how a token gets
    reissued/renewed, not how it gets un-revoked behind the caller's back
    (nothing calls `register()` again for a token nobody asked to renew)."""
    with _lock:
        data = _read()
        data[str(token_id)] = {
            "subject": subject,
            "scopes": list(scopes or ()),
            "expires_at": expires_at,
            "revoked_at": None,
            "created_at": time.time(),
        }
        _write(data)
        _revocation_cache.pop(str(token_id), None)


def revoke(token_id: str) -> bool:
    """Mark a service identity revoked right now. Returns False when the
    token has no service-identity record (e.g. an ordinary human token that
    was never `register()`ed — `authorize()`/`is_revoked()` only apply to
    tokens this module knows about)."""
    token_id = str(token_id)
    with _lock:
        data = _read()
        rec = data.get(token_id)
        if rec is None:
            return False
        rec["revoked_at"] = time.time()
        data[token_id] = rec
        _write(data)
        _revocation_cache[token_id] = (True, time.time())
    return True


def get(token_id: str) -> Optional[Dict[str, Any]]:
    with _lock:
        return _read().get(str(token_id))


def _revocation_bound_seconds() -> float:
    try:
        from src.settings import get_setting
        return max(0.0, float(get_setting("token_revocation_bound_seconds", 0) or 0))
    except Exception:
        return 0.0


def is_revoked(token_id: str) -> bool:
    """Whether this identity's revocation is visible right now.

    Bound = `token_revocation_bound_seconds` (default 0): with the default,
    the on-disk store is read fresh on every call — a `revoke()` is visible
    to the very next `is_revoked()` with zero propagation delay. A positive
    bound trades a short window in which a cached "not revoked" answer may
    be stale for fewer store reads under load; that value IS the promised
    bound, not an approximation of it.
    """
    token_id = str(token_id)
    bound = _revocation_bound_seconds()
    now = time.time()
    if bound > 0:
        cached = _revocation_cache.get(token_id)
        if cached is not None and (now - cached[1]) < bound:
            return cached[0]
    rec = get(token_id)
    revoked = bool(rec and rec.get("revoked_at"))
    if bound > 0:
        _revocation_cache[token_id] = (revoked, now)
    return revoked


def is_expired(token_id: str, *, now: Optional[float] = None) -> bool:
    rec = get(token_id)
    if not rec or rec.get("expires_at") is None:
        return False
    return (now if now is not None else time.time()) >= float(rec["expires_at"])


def check_scope(required_scope: str, held_scopes: List[str]) -> Tuple[bool, str]:
    """(allowed, reason). The reason names the exact missing scope, matching
    `core.authz.api_token_allowed`'s wording convention so a caller's 403
    body reads the same way regardless of which layer produced it."""
    held = {str(s).strip() for s in (held_scopes or ()) if str(s).strip()}
    if required_scope in held:
        return True, ""
    return False, f"Service credential missing required scope: {required_scope}"


def authorize(token_id: str, required_scope: str, held_scopes: List[str]) -> None:
    """Raise the exact `HTTPException` an unattended job's request should
    get for this credential.

    Order matters: revoked/expired is checked BEFORE scope, so a dead
    credential always gets 401 and never leaks which scope it would have
    needed if it were still alive. A live credential missing the required
    scope gets 403 naming that scope.
    """
    from fastapi import HTTPException

    if is_revoked(token_id):
        raise HTTPException(401, "Service credential revoked")
    if is_expired(token_id):
        raise HTTPException(401, "Service credential expired")
    ok, why = check_scope(required_scope, held_scopes)
    if not ok:
        raise HTTPException(403, why)
