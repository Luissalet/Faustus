"""work_lease.py — exclusive claims on background work, with fencing.

Ten chats that finish together should not start ten incompatible tidy-ups of
the same memory, and a tidy-up that stalled past its deadline must not publish
over the one that replaced it. This module is the claim those jobs take first:

* ``acquire(scope, holder)`` gives a :class:`Lease` when nobody holds the scope
  (or the holder's lease expired), and a :class:`LeaseDenied` saying why when
  not. Every grant carries a **fence**: a number that only grows for the scope.
* ``commit_window(lease)`` is the only safe place to publish. It holds the
  store's write lock while the caller writes, and reports ``ok=False`` when a
  newer lease was issued, so a worker that stalled past its deadline cannot
  publish after its successor took over: taking over needs that same lock.
* ``release(lease, success=..., input_revision=..., algorithm_version=...)``
  records a **watermark** only on success and only for the current fence. A
  failure schedules a retry time with exponential backoff instead.
* ``is_unchanged(scope, input_revision, algorithm_version)`` answers "has
  anything new arrived since the last successful run?", so a job whose input
  did not change ends without doing its work.

Same shape as ``src/budget_account.py``: a private SQLite file under
``DATA_DIR`` with ``BEGIN IMMEDIATE`` on every mutation and a guaranteed
``close()`` (Windows keeps the file locked otherwise). Leases work across the
processes that share that file: the app and an MCP server, for example.
"""

from __future__ import annotations

import json
import logging
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Union

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 600.0
BACKOFF_BASE_SECONDS = 30.0
BACKOFF_CAP_SECONDS = 3600.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS scopes (
    scope TEXT PRIMARY KEY,
    last_fence INTEGER NOT NULL DEFAULT 0,
    failures INTEGER NOT NULL DEFAULT 0,
    retry_after REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS leases (
    scope TEXT PRIMARY KEY,
    holder TEXT NOT NULL,
    token TEXT NOT NULL,
    fence INTEGER NOT NULL,
    acquired_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS watermarks (
    scope TEXT PRIMARY KEY,
    input_revision TEXT NOT NULL,
    algorithm_version TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '',
    completed_at REAL NOT NULL,
    fence INTEGER NOT NULL
);
"""


def default_path() -> Path:
    from src.constants import DATA_DIR
    return Path(DATA_DIR) / "work_leases.sqlite3"


@contextmanager
def _connect(path: Optional[Path] = None) -> Iterator[sqlite3.Connection]:
    target = Path(path) if path is not None else default_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_SCHEMA)
        yield conn
    finally:
        conn.close()


@contextmanager
def _write(path: Optional[Path] = None) -> Iterator[sqlite3.Connection]:
    with _connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")


@dataclass(frozen=True)
class Lease:
    scope: str
    holder: str
    token: str
    fence: int
    expires_at: float
    path: Optional[Path] = None


@dataclass(frozen=True)
class LeaseDenied:
    scope: str
    reason: str          # "held" or "backoff"
    until: float         # when it is worth asking again
    held_by: str = ""

    @property
    def message(self) -> str:
        if self.reason == "backoff":
            return f"{self.scope} is backing off after a failed run"
        return f"{self.scope} is already being worked on by {self.held_by or 'another worker'}"


def acquire(scope: str, holder: str, *, ttl: float = DEFAULT_TTL_SECONDS,
            now: Optional[float] = None, path: Optional[Path] = None) -> Union[Lease, LeaseDenied]:
    """Claim ``scope``. Never blocks: a busy scope answers at once."""
    if not scope:
        raise ValueError("a lease needs a scope")
    now = time.time() if now is None else now
    with _write(path) as conn:
        state = conn.execute(
            "SELECT last_fence, failures, retry_after FROM scopes WHERE scope=?", (scope,)).fetchone()
        last_fence = int(state["last_fence"]) if state else 0
        retry_after = float(state["retry_after"]) if state else 0.0
        if retry_after > now:
            return LeaseDenied(scope, "backoff", retry_after)
        current = conn.execute(
            "SELECT holder, expires_at FROM leases WHERE scope=?", (scope,)).fetchone()
        if current is not None and float(current["expires_at"]) > now:
            return LeaseDenied(scope, "held", float(current["expires_at"]), str(current["holder"]))
        fence = last_fence + 1
        token = secrets.token_hex(12)
        expires = now + max(1.0, float(ttl))
        conn.execute(
            "INSERT INTO scopes (scope, last_fence) VALUES (?, ?) "
            "ON CONFLICT(scope) DO UPDATE SET last_fence=excluded.last_fence", (scope, fence))
        conn.execute(
            "INSERT OR REPLACE INTO leases (scope, holder, token, fence, acquired_at, expires_at) "
            "VALUES (?,?,?,?,?,?)", (scope, holder, token, fence, now, expires))
    return Lease(scope, holder, token, fence, expires, Path(path) if path is not None else None)


def renew(lease: Lease, *, ttl: float = DEFAULT_TTL_SECONDS, now: Optional[float] = None) -> bool:
    """Extend a lease still held; False when a successor has taken it."""
    now = time.time() if now is None else now
    with _write(lease.path) as conn:
        cur = conn.execute(
            "UPDATE leases SET expires_at=? WHERE scope=? AND token=? AND fence=?",
            (now + max(1.0, float(ttl)), lease.scope, lease.token, lease.fence))
        return cur.rowcount > 0


def is_current(lease: Lease) -> bool:
    with _connect(lease.path) as conn:
        row = conn.execute(
            "SELECT 1 FROM leases WHERE scope=? AND token=? AND fence=?",
            (lease.scope, lease.token, lease.fence)).fetchone()
    return row is not None


class CommitWindow:
    """``ok`` is True while this lease is still the newest one for its scope."""

    def __init__(self, ok: bool, lease: Lease):
        self.ok = ok
        self.lease = lease


@contextmanager
def commit_window(lease: Lease) -> Iterator[CommitWindow]:
    """Hold the write lock while the caller publishes its result.

    Checked and held under one lock, so a successor cannot be granted between
    the check and the publish. Keep the body short: writers wait on it.
    """
    with _write(lease.path) as conn:
        row = conn.execute(
            "SELECT 1 FROM leases WHERE scope=? AND token=? AND fence=?",
            (lease.scope, lease.token, lease.fence)).fetchone()
        yield CommitWindow(row is not None, lease)


def release(lease: Lease, *, success: bool, input_revision: str = "",
            algorithm_version: str = "", payload: Optional[Dict[str, Any]] = None,
            now: Optional[float] = None, backoff: bool = True) -> bool:
    """End the lease. True only when it was still the current one.

    Success records the watermark and clears the failure count; failure
    schedules the next attempt with exponential backoff (``backoff=False`` for
    cheap, deterministic work where waiting buys nothing). A lease that was
    already replaced records nothing: its successor owns the scope's state.
    """
    now = time.time() if now is None else now
    with _write(lease.path) as conn:
        cur = conn.execute(
            "DELETE FROM leases WHERE scope=? AND token=? AND fence=?",
            (lease.scope, lease.token, lease.fence))
        if cur.rowcount == 0:
            return False
        if success:
            conn.execute(
                "INSERT OR REPLACE INTO watermarks "
                "(scope, input_revision, algorithm_version, payload, completed_at, fence) "
                "VALUES (?,?,?,?,?,?)",
                (lease.scope, input_revision, algorithm_version,
                 json.dumps(payload or {}, sort_keys=True), now, lease.fence))
            conn.execute("UPDATE scopes SET failures=0, retry_after=0 WHERE scope=?", (lease.scope,))
        elif backoff:
            row = conn.execute("SELECT failures FROM scopes WHERE scope=?", (lease.scope,)).fetchone()
            failures = (int(row["failures"]) if row else 0) + 1
            delay = min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * (2 ** (failures - 1)))
            conn.execute("UPDATE scopes SET failures=?, retry_after=? WHERE scope=?",
                         (failures, now + delay, lease.scope))
    return True


def watermark(scope: str, *, path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    with _connect(path) as conn:
        row = conn.execute("SELECT * FROM watermarks WHERE scope=?", (scope,)).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row["payload"] or "{}")
    except ValueError:
        payload = {}
    return {"input_revision": row["input_revision"], "algorithm_version": row["algorithm_version"],
            "payload": payload, "completed_at": row["completed_at"], "fence": row["fence"]}


def is_unchanged(scope: str, input_revision: str, algorithm_version: str,
                 *, path: Optional[Path] = None) -> bool:
    """True when the last successful run saw exactly this input with this algorithm."""
    mark = watermark(scope, path=path)
    return bool(mark and mark["input_revision"] == input_revision
                and mark["algorithm_version"] == algorithm_version)


def status(scope: Optional[str] = None, *, now: Optional[float] = None,
           path: Optional[Path] = None) -> list:
    """Inspection view: holder, fence, expiry, failures, backoff and watermark."""
    now = time.time() if now is None else now
    with _connect(path) as conn:
        scopes = conn.execute(
            "SELECT scope FROM scopes" + (" WHERE scope=?" if scope else "") + " ORDER BY scope",
            (scope,) if scope else ()).fetchall()
        rows = []
        for entry in scopes:
            name = entry["scope"]
            state = conn.execute("SELECT * FROM scopes WHERE scope=?", (name,)).fetchone()
            lease = conn.execute("SELECT * FROM leases WHERE scope=?", (name,)).fetchone()
            rows.append({
                "scope": name,
                "last_fence": state["last_fence"],
                "failures": state["failures"],
                "retry_after": state["retry_after"] or None,
                "backing_off": float(state["retry_after"]) > now,
                "held_by": lease["holder"] if lease else None,
                "lease_expires_at": lease["expires_at"] if lease else None,
                "lease_expired": bool(lease and float(lease["expires_at"]) <= now),
                "watermark": watermark(name, path=path),
            })
    return rows
