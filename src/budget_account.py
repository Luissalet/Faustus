"""budget_account.py — per-run token/cost budgeting for delegated work (A31).

A coordinator that hands work to children (subagent workers, a reviewer, a
judge) has no way today to say "you may spend at most this much" before the
child starts, or to find out afterwards what was actually spent once retries
and parallel children are added up. This module is that ledger: one
``BudgetAccount`` row per run (``open``), a RESERVATION taken before a child
is launched (``reserve`` — rejected up front, before the child ever runs, if
it would not fit under the ceiling), and a RECONCILIATION once the child's
real usage is known (``reconcile`` — the difference between what was
reserved and what was actually used is released back to the run).

Same shape as ``src/chat_outbox.py``: a private SQLite file under
``DATA_DIR`` rather than a table bolted onto ``core/database.py``, opened
read-only for a lookup and read-write (with a real ``BEGIN IMMEDIATE`` and a
guaranteed ``conn.close()``) for a mutation — Windows needs the connection
actually closed, not merely committed, or the file stays locked.

Vocabulary, exactly as the acceptance case names it:
  * ``ceiling``  — the run's token (and, optionally, cost) budget. ``0``
    means "no ceiling, accounting only" (the project-wide convention for a
    limit setting — see ``agent_budget_tokens_per_run`` in ``settings.py``).
  * ``reserved`` — tokens/cost set aside for a child that has not yet
    reported real usage.
  * ``consumed`` — tokens/cost a child actually used, once reconciled.
  * ``unpriced_usage`` — tokens a provider gave no price for. This is never
    folded into ``consumed_cost`` as if it were free: a ``snapshot`` that has
    any unpriced usage reports its cost state as ``"unknown"``, never as a
    known ``0.0``.

The sum a ceiling check compares against is orchestrator-visible spend as a
whole: every still-open reservation (children currently running, including
parallel ones) plus every already-reconciled consumption (finished children,
retries included — a retry is just another child_id/round reserving and
reconciling its own row, so its usage adds rather than overwrites).
"""
from __future__ import annotations

import logging
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

VERSION = 1


def default_path() -> Path:
    from src.constants import DATA_DIR
    return Path(DATA_DIR) / "budget_accounts.sqlite3"


@contextmanager
def _db(path: Path, *, write: bool = False):
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
    uri = path.resolve().as_uri() + ("?mode=rwc" if write else "?mode=ro")
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5)
    except sqlite3.OperationalError:
        # Read-only open against a file that does not exist yet: there is
        # nothing to read, so hand back an empty in-memory connection with
        # the same schema rather than raising on every snapshot() of a run
        # nobody has opened.
        if write:
            raise
        conn = sqlite3.connect(":memory:")
        _create_schema(conn)
        try:
            yield conn
        finally:
            conn.close()
        return
    conn.row_factory = sqlite3.Row
    try:
        if write:
            conn.execute("BEGIN IMMEDIATE")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version == 0 and write:
            if conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
                raise RuntimeError("unrecognized budget_accounts database")
            _create_schema(conn)
            conn.execute(f"PRAGMA user_version={VERSION}")
        elif version != VERSION and version != 0:
            raise RuntimeError("unsupported budget_accounts database version")
        yield conn
        if write:
            conn.commit()
    except Exception:
        if write:
            conn.rollback()
        raise
    finally:
        conn.close()


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE runs (
        run_id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL DEFAULT '',
        ceiling_tokens INTEGER NOT NULL DEFAULT 0,
        ceiling_cost REAL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL)""")
    conn.execute("""CREATE TABLE reservations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        child_id TEXT NOT NULL,
        status TEXT NOT NULL,
        reserved_tokens INTEGER NOT NULL DEFAULT 0,
        reserved_cost REAL,
        consumed_tokens INTEGER NOT NULL DEFAULT 0,
        consumed_cost REAL,
        unpriced INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL)""")
    conn.execute("CREATE INDEX reservations_run ON reservations(run_id, status)")
    conn.execute("CREATE INDEX reservations_child ON reservations(run_id, child_id)")


@dataclass
class Reservation:
    """A held slice of a run's budget, pending reconciliation."""
    reservation_id: int
    run_id: str
    child_id: str
    reserved_tokens: int
    reserved_cost: Optional[float] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "reservation_id": self.reservation_id, "run_id": self.run_id,
            "child_id": self.child_id, "reserved_tokens": self.reserved_tokens,
            "reserved_cost": self.reserved_cost,
        }


@dataclass
class BudgetExceeded:
    """Returned (never raised) when a reservation would not fit under the
    run's ceiling. The parent decides what to do next — shrink the ask, wait
    for another child to finish and free its reservation, or refuse the
    delegation outright — with the numbers to make that call in hand."""
    run_id: str
    child_id: str
    requested_tokens: int
    ceiling_tokens: int
    outstanding_tokens: int
    available_tokens: int
    reason: str = field(init=False)

    def __post_init__(self) -> None:
        self.reason = (
            f"budget exceeded for run {self.run_id}: child {self.child_id!r} requested "
            f"{self.requested_tokens} tokens but only {max(0, self.available_tokens)} remain "
            f"under the {self.ceiling_tokens}-token ceiling ({self.outstanding_tokens} already "
            "reserved/consumed)"
        )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "error": "budget_exceeded", "run_id": self.run_id, "child_id": self.child_id,
            "requested_tokens": self.requested_tokens, "ceiling_tokens": self.ceiling_tokens,
            "outstanding_tokens": self.outstanding_tokens,
            "available_tokens": self.available_tokens, "reason": self.reason,
        }


def open(run_id: str, ceiling_tokens: int = 0, ceiling_cost: Optional[float] = None,
         *, project_id: str = "") -> None:
    """Ensure a ``BudgetAccount`` row exists for ``run_id``. Idempotent — a
    second ``open`` for the same run is a no-op (the ceiling set by the
    first caller wins; a run does not get a wider budget just because a
    child later opened it again with a bigger number)."""
    if not run_id:
        raise ValueError("budget_account.open: run_id is required")
    now = time.time()
    path = default_path()
    with _db(path, write=True) as conn:
        conn.execute(
            "INSERT INTO runs (run_id, project_id, ceiling_tokens, ceiling_cost, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(run_id) DO NOTHING",
            (run_id, project_id, int(ceiling_tokens or 0), ceiling_cost, now, now),
        )


def _outstanding_tokens(conn: sqlite3.Connection, run_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(reserved_tokens), 0) AS reserved, "
        "COALESCE(SUM(consumed_tokens), 0) AS consumed FROM reservations "
        "WHERE run_id=? AND status='reserved'", (run_id,),
    ).fetchone()
    reserved = int(row["reserved"] or 0)
    consumed_row = conn.execute(
        "SELECT COALESCE(SUM(consumed_tokens), 0) AS consumed FROM reservations "
        "WHERE run_id=? AND status='reconciled'", (run_id,),
    ).fetchone()
    return reserved + int(consumed_row["consumed"] or 0)


def reserve(run_id: str, child_id: str, tokens: int, cost: Optional[float] = None):
    """Reserve up to ``tokens`` (and, if known, ``cost``) for ``child_id``
    before it is launched. Returns the :class:`Reservation` on success, or a
    :class:`BudgetExceeded` (never raised — the caller decides what to do)
    when the run has a ceiling and this reservation would not fit under it.

    A run that was never ``open``-ed gets an implicit ceiling of 0
    (unlimited, accounting only) rather than failing — a worker launched
    before its coordinator's budget setup ran must not crash on it."""
    if not run_id:
        raise ValueError("budget_account.reserve: run_id is required")
    if not child_id:
        raise ValueError("budget_account.reserve: child_id is required")
    tokens = max(0, int(tokens or 0))
    now = time.time()
    path = default_path()
    with _db(path, write=True) as conn:
        run_row = conn.execute(
            "SELECT ceiling_tokens FROM runs WHERE run_id=?", (run_id,),
        ).fetchone()
        if run_row is None:
            conn.execute(
                "INSERT INTO runs (run_id, project_id, ceiling_tokens, ceiling_cost, created_at, updated_at) "
                "VALUES (?, '', 0, NULL, ?, ?)", (run_id, now, now),
            )
            ceiling_tokens = 0
        else:
            ceiling_tokens = int(run_row["ceiling_tokens"] or 0)
        if ceiling_tokens > 0:
            outstanding = _outstanding_tokens(conn, run_id)
            available = ceiling_tokens - outstanding
            if tokens > max(0, available):
                return BudgetExceeded(
                    run_id=run_id, child_id=child_id, requested_tokens=tokens,
                    ceiling_tokens=ceiling_tokens, outstanding_tokens=outstanding,
                    available_tokens=available,
                )
        cur = conn.execute(
            "INSERT INTO reservations (run_id, child_id, status, reserved_tokens, reserved_cost, "
            "consumed_tokens, consumed_cost, unpriced, created_at, updated_at) "
            "VALUES (?, ?, 'reserved', ?, ?, 0, NULL, 0, ?, ?)",
            (run_id, child_id, tokens, cost, now, now),
        )
        reservation_id = int(cur.lastrowid)
        conn.execute("UPDATE runs SET updated_at=? WHERE run_id=?", (now, run_id))
    return Reservation(reservation_id=reservation_id, run_id=run_id, child_id=child_id,
                        reserved_tokens=tokens, reserved_cost=cost)


def reconcile(run_id: str, child_id: str, used_tokens: int,
              used_cost: Optional[float] = None) -> Dict[str, Any]:
    """Record what ``child_id`` actually used, closing its most recent open
    reservation (if any) so the surplus between reserved and used is freed
    back to the run. ``used_cost=None`` means the provider gave no price —
    that usage is booked as ``unpriced_usage``, never silently as a known
    ``0.0`` cost.

    A child that consumes without ever having been reserved for (a caller
    that skipped ``reserve``, or a retry counted purely for accounting)
    still gets a row here — reconciling into nothing would lose the usage
    from every future ``snapshot``."""
    if not run_id:
        raise ValueError("budget_account.reconcile: run_id is required")
    if not child_id:
        raise ValueError("budget_account.reconcile: child_id is required")
    used_tokens = max(0, int(used_tokens or 0))
    unpriced = 1 if used_cost is None else 0
    now = time.time()
    path = default_path()
    with _db(path, write=True) as conn:
        row = conn.execute(
            "SELECT id FROM reservations WHERE run_id=? AND child_id=? AND status='reserved' "
            "ORDER BY id DESC LIMIT 1", (run_id, child_id),
        ).fetchone()
        if row is not None:
            conn.execute(
                "UPDATE reservations SET status='reconciled', consumed_tokens=?, consumed_cost=?, "
                "unpriced=?, updated_at=? WHERE id=?",
                (used_tokens, used_cost, unpriced, now, int(row["id"])),
            )
            reservation_id = int(row["id"])
        else:
            cur = conn.execute(
                "INSERT INTO reservations (run_id, child_id, status, reserved_tokens, reserved_cost, "
                "consumed_tokens, consumed_cost, unpriced, created_at, updated_at) "
                "VALUES (?, ?, 'reconciled', ?, NULL, ?, ?, ?, ?, ?)",
                (run_id, child_id, used_tokens, used_tokens, used_cost, unpriced, now, now),
            )
            reservation_id = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO runs (run_id, project_id, ceiling_tokens, ceiling_cost, created_at, updated_at) "
            "VALUES (?, '', 0, NULL, ?, ?) ON CONFLICT(run_id) DO UPDATE SET updated_at=excluded.updated_at",
            (run_id, now, now),
        )
    return {"reservation_id": reservation_id, "run_id": run_id, "child_id": child_id,
            "consumed_tokens": used_tokens, "consumed_cost": used_cost, "unpriced": bool(unpriced)}


def snapshot(run_id: str) -> Dict[str, Any]:
    """The run's budget state: ceiling, what is still reserved (children in
    flight), what has been consumed (children finished, retries included),
    and the unpriced share of that consumption. ``remaining_tokens`` is
    ``None`` when the run has no ceiling (accounting only)."""
    path = default_path()
    with _db(path, write=False) as conn:
        run_row = conn.execute(
            "SELECT * FROM runs WHERE run_id=?", (run_id,),
        ).fetchone()
        rows: List[sqlite3.Row] = list(conn.execute(
            "SELECT * FROM reservations WHERE run_id=? ORDER BY id ASC", (run_id,),
        ))
    ceiling_tokens = int(run_row["ceiling_tokens"] or 0) if run_row is not None else 0
    ceiling_cost = (run_row["ceiling_cost"] if run_row is not None else None)
    reserved_tokens = sum(int(r["reserved_tokens"] or 0) for r in rows if r["status"] == "reserved")
    consumed_tokens = sum(int(r["consumed_tokens"] or 0) for r in rows if r["status"] == "reconciled")
    unpriced_tokens = sum(int(r["consumed_tokens"] or 0) for r in rows
                           if r["status"] == "reconciled" and r["unpriced"])
    known_cost = sum(float(r["consumed_cost"]) for r in rows
                      if r["status"] == "reconciled" and r["consumed_cost"] is not None)
    has_unpriced = unpriced_tokens > 0
    outstanding = reserved_tokens + consumed_tokens
    children = [
        {
            "child_id": r["child_id"], "status": r["status"],
            "reserved_tokens": int(r["reserved_tokens"] or 0),
            "reserved_cost": r["reserved_cost"],
            "consumed_tokens": int(r["consumed_tokens"] or 0),
            "consumed_cost": r["consumed_cost"],
            "unpriced": bool(r["unpriced"]),
        }
        for r in rows
    ]
    return {
        "run_id": run_id,
        "opened": run_row is not None,
        "ceiling_tokens": ceiling_tokens,
        "ceiling_cost": ceiling_cost,
        "reserved_tokens": reserved_tokens,
        "consumed_tokens": consumed_tokens,
        # "unknown" (never a known 0.0) the moment any consumed usage had no
        # price — a provider that gives no price is not free.
        "consumed_cost": ("unknown" if has_unpriced else (known_cost if rows else 0.0)),
        "unpriced_usage_tokens": unpriced_tokens,
        "remaining_tokens": (ceiling_tokens - outstanding) if ceiling_tokens > 0 else None,
        "children": children,
    }


def release(run_id: str, child_id: str) -> None:
    """Cancel an outstanding reservation with nothing consumed (the child
    never ran — e.g. a sibling's reservation already exhausted the budget
    and this one was withdrawn before launch). Distinct from ``reconcile``
    with ``used_tokens=0``: this leaves no unpriced/priced usage record at
    all, because none was ever incurred."""
    if not run_id or not child_id:
        return
    now = time.time()
    path = default_path()
    with _db(path, write=True) as conn:
        conn.execute(
            "UPDATE reservations SET status='released', reserved_tokens=0, updated_at=? "
            "WHERE run_id=? AND child_id=? AND status='reserved'",
            (now, run_id, child_id),
        )
