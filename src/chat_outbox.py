"""chat_outbox.py — the durable record behind chat send idempotency (TASK-03, UX-02).

A chat turn used to have no memory of its own submission: a double click, a
retried fetch after a dropped connection, or a page reload during a send all
looked identical to the server as a brand-new POST, and each one started a
brand-new turn. This module is the diary that closes that gap — one row per
`(owner, session_id, client_message_id)`, written *before* the turn starts and
updated once it settles, so a second POST carrying the same id can be answered
from the row instead of doing the work twice.

Same shape as `src/changeset_store.py`: a private, owner-scoped SQLite file of
its own rather than a table added to `core/database.py`. Nothing here decides
*whether* two sends share an id — the client mints it once per composed
message and the route below is what looks the id up before acting on it.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

VERSION = 1
MAX_RESULT_BYTES = 200_000

#: How long a settled (finished/failed) row is kept before `purge_stale` drops
#: it — long enough to answer a slow retry, short enough that the table stays
#: small. An unsettled row (accepted/running) outlives its own process crash
#: only for this long before it stops blocking a fresh retry.
FINISHED_TTL_SECONDS = 24 * 60 * 60
UNSETTLED_TTL_SECONDS = 12 * 60 * 60

#: Statuses a client-message-id can be in. `accepted` is the intent, written
#: before the turn does anything; `running` names the run once one exists;
#: `finished`/`failed` are terminal and carry whatever result there is.
STATUSES = ("accepted", "running", "finished", "failed")

_PURGE_INTERVAL_SECONDS = 60 * 60
_last_purge = 0.0


def default_path() -> Path:
    from src.constants import DATA_DIR
    return Path(DATA_DIR) / "chat_outbox.sqlite3"


def _now() -> str:
    from src.contracts.base import now_iso
    return now_iso()


@contextmanager
def _db(path: Path, *, write: bool = False):
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
    uri = path.resolve().as_uri() + ("?mode=rwc" if write else "?mode=ro")
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        if write:
            conn.execute("BEGIN IMMEDIATE")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version == 0 and write:
            if conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
                raise RuntimeError("unrecognized chat_outbox database")
            conn.execute("""CREATE TABLE outbox (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                owner TEXT NOT NULL, session_id TEXT NOT NULL,
                client_message_id TEXT NOT NULL,
                status TEXT NOT NULL, run_id TEXT NOT NULL DEFAULT '',
                result_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
            conn.execute("CREATE UNIQUE INDEX outbox_identity "
                         "ON outbox(owner, session_id, client_message_id)")
            conn.execute("CREATE INDEX outbox_updated ON outbox(status, updated_at)")
            conn.execute(f"PRAGMA user_version={VERSION}")
        elif version != VERSION:
            raise RuntimeError("unsupported chat_outbox database version")
        yield conn
        if write:
            conn.commit()
    except Exception:
        if write:
            conn.rollback()
        raise
    finally:
        conn.close()


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    result = None
    if row["result_json"]:
        try:
            result = json.loads(row["result_json"])
        except (TypeError, ValueError):
            result = None
    return {
        "owner": row["owner"], "session_id": row["session_id"],
        "client_message_id": row["client_message_id"],
        "status": row["status"], "run_id": row["run_id"] or "",
        "result": result,
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def record_intent(*, owner: str, session_id: str, client_message_id: str) -> Dict[str, Any]:
    """Write the row that says a turn for this id is about to start.

    Idempotent by construction: the unique index on
    `(owner, session_id, client_message_id)` means a race between two
    concurrent first-sends loses on the index rather than opening two
    accepted rows, and the loser gets back the winner's row with
    `created=False` instead of an error.
    """
    _maybe_purge()
    path = default_path()
    stamp = _now()
    with _db(path, write=True) as conn:
        try:
            conn.execute(
                "INSERT INTO outbox (owner, session_id, client_message_id, status, "
                "run_id, result_json, created_at, updated_at) VALUES (?, ?, ?, 'accepted', '', NULL, ?, ?)",
                (owner, session_id, client_message_id, stamp, stamp),
            )
        except sqlite3.IntegrityError:
            row = conn.execute(
                "SELECT * FROM outbox WHERE owner=? AND session_id=? AND client_message_id=?",
                (owner, session_id, client_message_id),
            ).fetchone()
            if row is None:  # pragma: no cover - lost the row between the insert and the reread
                raise
            out = _row_to_dict(row)
            out["created"] = False
            return out
    return {"owner": owner, "session_id": session_id, "client_message_id": client_message_id,
            "status": "accepted", "run_id": "", "result": None,
            "created_at": stamp, "updated_at": stamp, "created": True}


def get(*, owner: str, session_id: str, client_message_id: str) -> Optional[Dict[str, Any]]:
    path = default_path()
    if not path.exists():
        return None
    with _db(path) as conn:
        row = conn.execute(
            "SELECT * FROM outbox WHERE owner=? AND session_id=? AND client_message_id=?",
            (owner, session_id, client_message_id),
        ).fetchone()
        return _row_to_dict(row) if row is not None else None


def mark_running(*, owner: str, session_id: str, client_message_id: str, run_id: str) -> bool:
    """Name the run once one exists. Never moves a row backward out of a
    terminal state — a slow update racing a fast finish must not resurrect it."""
    path = default_path()
    stamp = _now()
    with _db(path, write=True) as conn:
        changed = conn.execute(
            "UPDATE outbox SET status='running', run_id=?, updated_at=? "
            "WHERE owner=? AND session_id=? AND client_message_id=? AND status='accepted'",
            (run_id, stamp, owner, session_id, client_message_id),
        ).rowcount
        return bool(changed)


def mark_finished(*, owner: str, session_id: str, client_message_id: str, status: str,
                  result: Optional[Dict[str, Any]] = None) -> bool:
    """Close the diary entry with what actually happened.

    `status` is 'finished' or 'failed' — never 'accepted'/'running' here, this
    is the "result after" half of TASK-03's "intent before, result after"."""
    if status not in ("finished", "failed"):
        raise ValueError(f"mark_finished needs a terminal status, got {status!r}")
    encoded = None
    if result is not None:
        try:
            encoded = json.dumps(result, ensure_ascii=False)
        except (TypeError, ValueError):
            encoded = None
        else:
            if len(encoded.encode("utf-8")) > MAX_RESULT_BYTES:
                encoded = None  # too big to replay verbatim; the status alone still answers a retry
    path = default_path()
    stamp = _now()
    with _db(path, write=True) as conn:
        changed = conn.execute(
            "UPDATE outbox SET status=?, result_json=?, updated_at=? "
            "WHERE owner=? AND session_id=? AND client_message_id=? AND status IN ('accepted','running')",
            (status, encoded, stamp, owner, session_id, client_message_id),
        ).rowcount
        return bool(changed)


def _maybe_purge(*, force: bool = False) -> None:
    global _last_purge
    now = time.monotonic()
    if not force and now - _last_purge < _PURGE_INTERVAL_SECONDS:
        return
    _last_purge = now
    try:
        purge_stale()
    except Exception as exc:  # pragma: no cover - cleanup is best-effort
        logger.debug("chat_outbox: opportunistic purge skipped (%s)", exc)


def purge_stale() -> int:
    """Drop rows too old to plausibly answer a retry. Safe to call any time,
    including concurrently with itself — it is a bounded DELETE, not a scan
    that holds state between calls."""
    path = default_path()
    if not path.exists():
        return 0

    def _iso_ago(seconds: int) -> str:
        from datetime import datetime, timedelta, timezone
        return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).replace(
            microsecond=0).isoformat().replace("+00:00", "Z")

    with _db(path, write=True) as conn:
        removed = conn.execute(
            "DELETE FROM outbox WHERE (status IN ('finished','failed') AND updated_at < ?) "
            "OR (status IN ('accepted','running') AND updated_at < ?)",
            (_iso_ago(FINISHED_TTL_SECONDS), _iso_ago(UNSETTLED_TTL_SECONDS)),
        ).rowcount
        return int(removed or 0)
