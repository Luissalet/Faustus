"""compaction_pins.py — CTX-02: fragments the user pinned, and a record of
what compaction actually did last.

``context_compactor.compact_with_integrity`` already never paraphrases an
identifier away; what it could not do is let a *user* say "never fold this
one, no matter what the deterministic rules would otherwise do" — a message
that is not the last turn, not a pasted code block, not an ``ask_user``
answer, but still matters enough that summarizing it away would lose
something the rules cannot see. This module is the store for that: a pin is
identified by the same row fingerprint ``context_compactor`` already computes
(role + length + flattened text), so pinning survives a session reload
without needing a stable message id that does not exist yet.

The second half is the other stated gap: nothing recorded what a compaction
pass actually folded, so a turn that lost a name for good was unrecoverable
without diffing history by hand. ``record_event``/``last_event`` are a small
append-and-read log of the ledger `compact_with_integrity` (and
`maybe_compact`) already produce — the marker text and the EvidenceRefs at
the untouched originals — scoped by owner and session, so a route can answer
"what did compaction do to this session, most recently" without touching
`agent_loop.py` at all: both compaction functions call in here themselves.

Schema lives in the shared Context Engine database (`store.py`) — this is
derived, rebuildable state exactly like blocks and capsules, not a new table
in `app.db`.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

from src.context_engine import store

logger = logging.getLogger(__name__)

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS compaction_pins (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner TEXT NOT NULL DEFAULT '',
        session_id TEXT NOT NULL DEFAULT '',
        fingerprint TEXT NOT NULL,
        excerpt TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        UNIQUE(owner, session_id, fingerprint)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_compaction_pins_scope "
    "ON compaction_pins(owner, session_id)",
    """
    CREATE TABLE IF NOT EXISTS compaction_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner TEXT NOT NULL DEFAULT '',
        session_id TEXT NOT NULL DEFAULT '',
        kind TEXT NOT NULL DEFAULT 'integrity',
        marker_text TEXT NOT NULL DEFAULT '',
        folded_count INTEGER NOT NULL DEFAULT 0,
        pinned_skipped INTEGER NOT NULL DEFAULT 0,
        tokens_before INTEGER NOT NULL DEFAULT 0,
        tokens_after INTEGER NOT NULL DEFAULT 0,
        evidence_refs TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_compaction_events_scope "
    "ON compaction_events(owner, session_id, created_at)",
)

store.register_schema("compaction_pins", _SCHEMA)

#: A pin store this large per (owner, session) is already a sign something
#: upstream is pinning indiscriminately; capped the same way blocks.py caps
#: `always_loaded` growth-by-mistake.
MAX_PINS_PER_SESSION = 200


def pin_fragment(owner: str, session_id: str, fingerprint: str,
                  excerpt: str = "") -> Dict[str, Any]:
    """Mark one message (by its row fingerprint) as never-folded for this
    session. Idempotent — pinning twice updates the excerpt, not the count."""
    fp = str(fingerprint or "").strip()
    if not fp:
        raise ValueError("fingerprint is required")
    sid = str(session_id or "").strip()
    if not sid:
        raise ValueError("session_id is required")
    with store.db() as conn:
        existing = conn.execute(
            "SELECT COUNT(*) AS n FROM compaction_pins WHERE owner=? AND session_id=?",
            (owner or "", sid),
        ).fetchone()
        if int(existing["n"]) >= MAX_PINS_PER_SESSION:
            row = conn.execute(
                "SELECT 1 FROM compaction_pins WHERE owner=? AND session_id=? AND fingerprint=?",
                (owner or "", sid, fp),
            ).fetchone()
            if row is None:
                raise ValueError(f"pin limit reached ({MAX_PINS_PER_SESSION}) for this session")
        conn.execute(
            "INSERT INTO compaction_pins(owner, session_id, fingerprint, excerpt, created_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(owner, session_id, fingerprint) DO UPDATE SET excerpt=excluded.excerpt",
            (owner or "", sid, fp, str(excerpt or "")[:2048], store.now_iso()),
        )
    return {"owner": owner or "", "session_id": sid, "fingerprint": fp,
            "excerpt": str(excerpt or "")[:2048]}


def unpin_fragment(owner: str, session_id: str, fingerprint: str) -> bool:
    fp = str(fingerprint or "").strip()
    sid = str(session_id or "").strip()
    if not fp or not sid:
        return False
    with store.db() as conn:
        cur = conn.execute(
            "DELETE FROM compaction_pins WHERE owner=? AND session_id=? AND fingerprint=?",
            (owner or "", sid, fp),
        )
        return cur.rowcount > 0


def list_pins(owner: str, session_id: str) -> List[Dict[str, Any]]:
    sid = str(session_id or "").strip()
    if not sid:
        return []
    with store.db() as conn:
        cur = conn.execute(
            "SELECT fingerprint, excerpt, created_at FROM compaction_pins "
            "WHERE owner=? AND session_id=? ORDER BY created_at ASC",
            (owner or "", sid),
        )
        return store.rows(cur)


def pinned_fingerprints(owner: str, session_id: str) -> set:
    """The bare set `compact_with_integrity` checks each row against. Never
    raises — a store outage means "nothing pinned", not a broken compaction."""
    try:
        return {row["fingerprint"] for row in list_pins(owner, session_id)}
    except Exception:  # noqa: BLE001 - pins are never load-bearing for compaction
        logger.debug("compaction_pins: could not load pins", exc_info=True)
        return set()


def record_event(owner: str, session_id: str, *, kind: str, marker_text: str,
                  folded_count: int, tokens_before: int, tokens_after: int,
                  evidence_refs: Optional[Sequence[Dict[str, Any]]] = None,
                  pinned_skipped: int = 0) -> None:
    """Append one row to the compaction log. Best-effort: a caller mid-turn
    (`context_compactor.py`) must never fail a compaction because the log
    could not be written."""
    sid = str(session_id or "").strip()
    if not sid:
        return
    try:
        with store.db() as conn:
            conn.execute(
                "INSERT INTO compaction_events(owner, session_id, kind, marker_text, "
                "folded_count, pinned_skipped, tokens_before, tokens_after, evidence_refs, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (owner or "", sid, str(kind or "integrity"), str(marker_text or "")[:8192],
                 int(folded_count or 0), int(pinned_skipped or 0),
                 int(tokens_before or 0), int(tokens_after or 0),
                 store.dumps(list(evidence_refs or [])), store.now_iso()),
            )
    except Exception:  # noqa: BLE001 - the log is diagnostic, not load-bearing
        logger.debug("compaction_pins: could not record a compaction event", exc_info=True)


def last_event(owner: str, session_id: str) -> Optional[Dict[str, Any]]:
    """The most recent compaction event for this session, or None when
    compaction has never run for it (or the log could not be read)."""
    sid = str(session_id or "").strip()
    if not sid:
        return None
    try:
        with store.db() as conn:
            row = conn.execute(
                "SELECT kind, marker_text, folded_count, pinned_skipped, tokens_before, "
                "tokens_after, evidence_refs, created_at FROM compaction_events "
                "WHERE owner=? AND session_id=? ORDER BY created_at DESC, id DESC LIMIT 1",
                (owner or "", sid),
            ).fetchone()
    except Exception:  # noqa: BLE001
        logger.debug("compaction_pins: could not read compaction events", exc_info=True)
        return None
    if row is None:
        return None
    out = dict(row)
    out["evidence_refs"] = store.loads_list(out.get("evidence_refs"))
    return out


__all__ = [
    "pin_fragment", "unpin_fragment", "list_pins", "pinned_fingerprints",
    "record_event", "last_event", "MAX_PINS_PER_SESSION",
]
