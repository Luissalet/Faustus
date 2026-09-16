"""consent.py — WP18: owner-scoped, scoped-and-expiring consent for a
voice/likeness clone.

`src/media_consent.py` (MEDIA-06) already answers "did *anyone* obtain a
yes to clone this subject" — a single global JSON registry, `subject` free
text, no owner scoping, no expiry, no evidence hash. `src.creator.preflight`
(WP09) already reads it via `_missing_consent()` for the `voice_clone`/`dub`
operation axis, and that wiring is not this lot's to touch (CONTRATO.md:
WP09 owns `preflight.py`).

AUD10's own acceptance criterion is "ampliar el gate actual con alcance,
vigencia, hashes de referencias y autor humano" — DEEPEN the existing gate,
not replace it. This module is that deepening: a Creator-scoped registry
keyed by ``(owner, subject, scope)`` that adds what `media_consent` has no
room for — an expiry (``expires_at``), a hash/URL of the evidence the human
reviewer actually saw (``evidence_ref``), and per-owner isolation so one
account's consent record is never visible to (or revocable by) another's
preflight check.

It stays a *deepening*, not a parallel authority, two ways:

* Every :func:`register` here also calls `src.media_consent.register()`
  (best-effort — a bridge failure never blocks OR silently fakes the
  Creator-side record) so a plain `media_consent.has_consent(subject)` read
  — the one `src.creator.preflight._missing_consent()` already does — keeps
  seeing a live record the moment this module grants one. :func:`revoke`
  bridges the same way, revoking the EXACT `media_consent` row this module's
  `register()` created for it (tracked by id), never guessing which of
  possibly several free-text-matching rows to touch.
* This module's own :func:`is_valid` is what `src/creator/adapters/tts.py`
  actually gates `voice_clone`/`dub` `submit()` on — checked FRESH at
  submit time, never cached from an earlier preflight — because AUD10's
  acceptance is explicit: "un permiso vencido se comprueba antes de
  ejecutar, aunque el preflight anterior fuera válido", and "revocar
  bloquea trabajos aún no autorizados a ejecutar". A `media_consent`-only
  check (no expiry) cannot honour the first half of that on its own.

Persistence (CONTRATO.md rule 2): sqlite at ``DATA_DIR/creator/consent.db``,
one connection opened and closed per call, WAL, a real ``BEGIN IMMEDIATE``
for every write — same pattern as ``src/creator/store.py`` /
``src/budget_account.py``. Records are appended and revoked in place
(``revoked_at`` set), never deleted — a consent decision is exactly the
kind of small, rare, human-reviewed record CONTRATO.md rule 2 says stays
append-only rather than mutated away.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_BUSY_TIMEOUT_S = 30
_SCHEMA_LOCK = threading.Lock()

#: Every scope this registry has a documented meaning for. Free text is
#: still accepted (a caller may need a scope this module has not named yet)
#: but these are the ones `src/creator/adapters/tts.py` and the routes know
#: to ask about by default.
SCOPES = ("clone", "dub", "likeness")


def default_path() -> str:
    from src.constants import DATA_DIR
    return os.path.join(DATA_DIR, "creator", "consent.db")


@contextmanager
def _conn(db_path: str, *, immediate: bool = False):
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=_BUSY_TIMEOUT_S, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        if immediate:
            conn.execute("BEGIN IMMEDIATE")
        yield conn
        if immediate:
            conn.execute("COMMIT")
    except Exception:
        if immediate:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        raise
    finally:
        conn.close()


def _ensure_schema(db_path: str) -> None:
    with _SCHEMA_LOCK, _conn(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS creator_consent (
                id TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                subject TEXT NOT NULL,
                scope TEXT NOT NULL,
                granted_by TEXT NOT NULL,
                evidence_ref TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',
                granted_at REAL NOT NULL,
                expires_at REAL,
                revoked_at REAL,
                media_consent_id TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_creator_consent_owner_subject "
            "ON creator_consent(owner, subject, scope)"
        )


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"], "owner": row["owner"], "subject": row["subject"],
        "scope": row["scope"], "granted_by": row["granted_by"],
        "evidence_ref": row["evidence_ref"], "note": row["note"],
        "granted_at": row["granted_at"], "expires_at": row["expires_at"],
        "revoked_at": row["revoked_at"],
        "status": _status(row["revoked_at"], row["expires_at"]),
    }


def _status(revoked_at: Optional[float], expires_at: Optional[float]) -> str:
    if revoked_at:
        return "revoked"
    if expires_at is not None and expires_at <= time.time():
        return "expired"
    return "active"


def _bridge_register(subject: str, *, granted_by: str, scope: str, note: str) -> str:
    """Best-effort mirror into `src.media_consent` — see module docstring.
    Returns the bridged record's id, or "" if the bridge itself failed (the
    Creator-side record is still created either way; a caller sees the
    bridge gap via `bridged_to_media_consent: False` in the return value
    rather than a hidden inconsistency)."""
    try:
        from src import media_consent
        result = media_consent.register(subject, granted_by=granted_by, scope=scope, note=note)
        if result.get("ok"):
            return str((result.get("consent") or {}).get("id") or "")
    except Exception:  # noqa: BLE001 - the bridge must never block the real record
        logger.exception("creator.consent: media_consent bridge register failed")
    return ""


def _bridge_revoke(media_consent_id: str) -> None:
    if not media_consent_id:
        return
    try:
        from src import media_consent
        media_consent.revoke(media_consent_id)
    except Exception:  # noqa: BLE001 - the bridge must never block the real revoke
        logger.exception("creator.consent: media_consent bridge revoke failed")


def register(owner: str, subject: str, *, granted_by: str, scope: str = "clone",
            evidence_ref: str = "", note: str = "", expires_at: Optional[float] = None,
            db_path: Optional[str] = None) -> Dict[str, Any]:
    """Record that `granted_by` obtained `owner`'s consent from `subject`
    for `scope`, valid until `expires_at` (a unix timestamp, or `None` for
    no expiry — "sin caducidad" is an explicit choice, not a default nobody
    made). `evidence_ref` is a hash or URL the reviewer can trace back to
    what was actually shown/signed — never the evidence bytes themselves
    (this registry holds decisions, not media)."""
    owner_key = (owner or "").strip()
    subject_key = (subject or "").strip()
    who = (granted_by or "").strip()
    scope = (scope or "clone").strip() or "clone"
    if not owner_key:
        return {"ok": False, "reason": "no_owner", "detail": "consent must be scoped to an owner"}
    if not subject_key:
        return {"ok": False, "reason": "no_subject",
                "detail": "name/identifier whose voice or likeness this covers"}
    if not who:
        return {"ok": False, "reason": "no_grantor",
                "detail": "a consent record has to say who obtained it"}
    if expires_at is not None:
        try:
            expires_at = float(expires_at)
        except (TypeError, ValueError):
            return {"ok": False, "reason": "invalid_expiry", "detail": "expires_at must be a number"}
        if expires_at <= time.time():
            return {"ok": False, "reason": "expiry_in_past",
                    "detail": "expires_at must be in the future"}

    db_path = db_path or default_path()
    _ensure_schema(db_path)
    consent_id = f"consent_{uuid.uuid4().hex[:20]}"
    stamp = time.time()
    media_consent_id = _bridge_register(subject_key, granted_by=who, scope=scope, note=note)

    with _conn(db_path, immediate=True) as conn:
        conn.execute(
            """
            INSERT INTO creator_consent
                (id, owner, subject, scope, granted_by, evidence_ref, note,
                 granted_at, expires_at, revoked_at, media_consent_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
            """,
            (consent_id, owner_key, subject_key, scope, who, evidence_ref or "",
             note or "", stamp, expires_at, media_consent_id),
        )
    record = {
        "id": consent_id, "owner": owner_key, "subject": subject_key, "scope": scope,
        "granted_by": who, "evidence_ref": evidence_ref or "", "note": note or "",
        "granted_at": stamp, "expires_at": expires_at, "revoked_at": None, "status": "active",
    }
    return {"ok": True, "consent": record, "bridged_to_media_consent": bool(media_consent_id)}


def revoke(owner: str, consent_id: str, *, db_path: Optional[str] = None) -> Dict[str, Any]:
    """Withdraw one record. Idempotent (a retried click is not a bug), and
    404-shaped for a foreign/unknown id — `owner` is checked, never trusted
    from a caller who merely knows the id (CONTRATO.md rule 3)."""
    db_path = db_path or default_path()
    _ensure_schema(db_path)
    owner_key = (owner or "").strip()
    with _conn(db_path, immediate=True) as conn:
        row = conn.execute(
            "SELECT * FROM creator_consent WHERE id = ? AND owner = ?",
            (consent_id, owner_key),
        ).fetchone()
        if row is None:
            return {"ok": False, "reason": "not_found"}
        if row["revoked_at"]:
            return {"ok": True, "reason": "already_revoked", "idempotent": True}
        stamp = time.time()
        conn.execute(
            "UPDATE creator_consent SET revoked_at = ? WHERE id = ?",
            (stamp, consent_id),
        )
        media_consent_id = row["media_consent_id"]
    _bridge_revoke(media_consent_id)
    return {"ok": True, "reason": "revoked", "revoked_at": stamp}


def is_valid(owner: str, subject: str, *, scope: str = "clone",
             db_path: Optional[str] = None) -> bool:
    """Is there a live (unrevoked, unexpired) record for `owner`+`subject`+
    `scope`, checked RIGHT NOW? See module docstring — this is the check
    `submit()` re-runs every time, never a value cached from an earlier
    preflight."""
    owner_key = (owner or "").strip()
    subject_key = (subject or "").strip()
    if not owner_key or not subject_key:
        return False
    db_path = db_path or default_path()
    _ensure_schema(db_path)
    now = time.time()
    with _conn(db_path) as conn:
        rows = conn.execute(
            "SELECT expires_at FROM creator_consent WHERE owner = ? AND subject = ? "
            "AND scope = ? AND revoked_at IS NULL",
            (owner_key, subject_key, scope or "clone"),
        ).fetchall()
    for row in rows:
        expires_at = row["expires_at"]
        if expires_at is None or expires_at > now:
            return True
    return False


def get(owner: str, consent_id: str, *, db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    db_path = db_path or default_path()
    _ensure_schema(db_path)
    with _conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM creator_consent WHERE id = ? AND owner = ?",
            (consent_id, (owner or "").strip()),
        ).fetchone()
    return _row_to_dict(row) if row is not None else None


def for_subject(owner: str, subject: str, *, db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Every record for one subject, newest first — the audit trail an
    AUD10 review needs, not just the current yes/no `is_valid()` answer."""
    db_path = db_path or default_path()
    _ensure_schema(db_path)
    with _conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM creator_consent WHERE owner = ? AND subject = ? "
            "ORDER BY granted_at DESC",
            ((owner or "").strip(), (subject or "").strip()),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def list_for_owner(owner: str, *, db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    db_path = db_path or default_path()
    _ensure_schema(db_path)
    with _conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM creator_consent WHERE owner = ? ORDER BY granted_at DESC",
            ((owner or "").strip(),),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


__all__ = [
    "SCOPES", "default_path", "register", "revoke", "is_valid", "get",
    "for_subject", "list_for_owner",
]
