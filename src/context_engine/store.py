"""
context_engine/store.py — one SQLite file, many owners, no core migration.

Everything the Context Engine persists is *derived*: blocks distilled from
project memory, capsules distilled from a run, experiences distilled from a
proven changeset, a symbol index distilled from files on disk.  Losing the
whole file costs a rebuild and nothing else.  That is precisely the property
that argues for a separate database rather than four new tables in `app.db`:

- a rebuild is `DELETE FROM …`, not a migration with a rollback plan;
- an index refresh that takes eight seconds cannot hold a write lock that a
  chat turn is waiting on;
- and when it does get corrupted — WAL on a Windows machine that lost power —
  quarantining it costs a reindex instead of the user's sessions.

`src.memory_engine` reached the same conclusion and its own `.db`; this is that
pattern generalised so that blocks, capsules, experiences, findings, recipes
and the packet ledger share one connection policy instead of inventing six.

Schema lives with the feature that owns it.  A module calls `register_schema()`
at import time and every connection opened afterwards applies it — all
statements are `IF NOT EXISTS`, so applying them on every open is a few
microseconds and removes the entire class of "the table was not created yet".
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from src.constants import CONTEXT_ENGINE_DB

logger = logging.getLogger(__name__)


class ContextStoreError(RuntimeError):
    """The store could not be opened or written.  Callers on the turn path are
    expected to catch this and degrade, never to propagate it into a chat."""


_LOCK = threading.RLock()
_SCHEMAS: "Dict[str, Tuple[str, ...]]" = {}
_PATH_OVERRIDE: Optional[str] = None


def register_schema(name: str, statements: Sequence[str]) -> None:
    """Declare the tables one feature owns.

    Re-registering the same name replaces it, which is what makes a module
    reload in a test suite harmless.  Statements must be idempotent
    (`CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`); they run on
    every connection."""
    with _LOCK:
        _SCHEMAS[str(name)] = tuple(str(s) for s in statements if str(s).strip())


def registered_schemas() -> Tuple[str, ...]:
    with _LOCK:
        return tuple(sorted(_SCHEMAS))


def db_path() -> str:
    return _PATH_OVERRIDE or CONTEXT_ENGINE_DB


def use_path(path: Optional[str]) -> None:
    """Point the store somewhere else.  Only tests and the doctor call this;
    it exists so a test can use `tmp_path` without monkeypatching a constant
    that a dozen modules have already imported by value."""
    global _PATH_OVERRIDE
    with _LOCK:
        _PATH_OVERRIDE = path


def _quarantine(path: str, reason: Any) -> None:
    """Move an unreadable database aside rather than deleting it.  A `.corrupt`
    copy is worth keeping: `sqlite3 .recover` succeeds more often than people
    expect, and the alternative is telling someone their index is simply gone."""
    for suffix in ("", "-wal", "-shm"):
        victim = path + suffix
        if not os.path.exists(victim):
            continue
        try:
            os.replace(victim, victim + ".corrupt")
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(victim)
    logger.warning("context_engine.db was unusable (%s); moved aside and recreated", reason)


def _apply_schema(conn: sqlite3.Connection) -> None:
    with _LOCK:
        blocks = list(_SCHEMAS.items())
    for name, statements in blocks:
        for statement in statements:
            try:
                conn.execute(statement)
            except sqlite3.Error as exc:
                # One feature's bad DDL must not take the whole store down with
                # it; the feature will fail loudly on its first query instead.
                logger.error("context_engine schema %s failed: %s", name, exc)


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10.0)
    try:
        conn.row_factory = sqlite3.Row
        with contextlib.suppress(sqlite3.Error):
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
        _apply_schema(conn)
        conn.commit()
        return conn
    except Exception:
        with contextlib.suppress(Exception):
            conn.close()
        raise


def _open() -> sqlite3.Connection:
    path = db_path()
    with contextlib.suppress(OSError):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    try:
        return _connect(path)
    except sqlite3.DatabaseError as exc:
        _quarantine(path, exc)
        try:
            return _connect(path)
        except sqlite3.Error as exc2:  # pragma: no cover - dead disk
            raise ContextStoreError(f"context engine store unusable: {exc2}") from exc2


@contextlib.contextmanager
def db():
    """A short-lived connection under the module lock, committed on success.

    Short-lived on purpose: a long-held connection on WAL keeps the -wal file
    growing and turns a crash into a slow recovery on next start."""
    with _LOCK:
        conn = _open()
        try:
            yield conn
            conn.commit()
        except Exception:
            with contextlib.suppress(Exception):
                conn.rollback()
            raise
        finally:
            with contextlib.suppress(Exception):
                conn.close()


# ── small shared helpers, so six modules do not write them six ways ────────

def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(value: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def age_seconds(value: Any, *, now: Optional[datetime] = None) -> Optional[float]:
    """How old a timestamp is, or None if it cannot be read.

    None is not zero.  An unreadable timestamp means "we do not know how fresh
    this is", and a freshness filter has to treat that differently from "brand
    new" — otherwise a corrupt field becomes a permanent pass."""
    parsed = parse_iso(value)
    if parsed is None:
        return None
    return max(0.0, ((now or datetime.now(timezone.utc)) - parsed).total_seconds())


def dumps(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return "null"


def loads(raw: Any, default: Any = None) -> Any:
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def loads_list(raw: Any) -> List[Any]:
    value = loads(raw, [])
    return value if isinstance(value, list) else []


def loads_dict(raw: Any) -> Dict[str, Any]:
    value = loads(raw, {})
    return value if isinstance(value, dict) else {}


def rows(cursor: Iterable[sqlite3.Row]) -> List[Dict[str, Any]]:
    return [dict(row) for row in cursor]


def scope_clause(owner: str = "", project_id: str = "", *,
                 alias: str = "", include_global: bool = True) -> Tuple[str, List[Any]]:
    """The owner filter, written once.

    `include_global=True` lets rows with an empty owner through — that is how a
    rule the whole install shares is stored, and `memory_engine.scoped_items`
    already works that way.  `include_global=False` is the strict form used
    wherever a leak between two people would be worse than a missing rule."""
    prefix = f"{alias}." if alias else ""
    where: List[str] = []
    params: List[Any] = []
    if include_global:
        where.append(f"({prefix}owner = ? OR {prefix}owner = '')")
        params.append(owner or "")
    else:
        where.append(f"{prefix}owner = ?")
        params.append(owner or "")
    if project_id:
        where.append(f"({prefix}project_id = ? OR {prefix}project_id = '')")
        params.append(project_id)
    return " AND ".join(where), params


def vacuum() -> None:
    """Reclaim space after a large delete.  Called by maintenance, never on the
    turn path — VACUUM takes an exclusive lock for as long as it takes."""
    with db() as conn:
        with contextlib.suppress(sqlite3.Error):
            conn.execute("VACUUM")


def table_counts() -> Dict[str, int]:
    """For diagnostics: how big is each table, right now.  A table that cannot
    be counted reports -1 rather than vanishing from the report."""
    out: Dict[str, int] = {}
    with db() as conn:
        names = [r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )]
        for name in names:
            try:
                out[name] = int(conn.execute(f"SELECT COUNT(*) AS n FROM \"{name}\"").fetchone()["n"])
            except sqlite3.Error:
                out[name] = -1
    return out


def store_bytes() -> int:
    """Size on disk including the write-ahead log, which is the number that
    actually surprises people."""
    total = 0
    for suffix in ("", "-wal", "-shm"):
        with contextlib.suppress(OSError):
            total += os.path.getsize(db_path() + suffix)
    return total


__all__ = [
    "ContextStoreError", "register_schema", "registered_schemas", "db_path",
    "use_path", "db", "now_iso", "parse_iso", "age_seconds", "dumps", "loads",
    "loads_list", "loads_dict", "rows", "scope_clause", "vacuum",
    "table_counts", "store_bytes",
]
