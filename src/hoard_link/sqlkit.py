"""One SQLite ``Database`` for every Hoard app (replaces 16 copies of the same ~70-line class).

Standard library only. The Node twin is ``openDatabase`` in ``js/hoard-commons/server.js`` (over ``node:sqlite``).

The family convention is kept: **one connection shared by every thread, guarded by one re-entrant lock**, rows as
``sqlite3.Row``, WAL, autocommit mode (``isolation_level=None``) so transactions are explicit. What changes:

* :meth:`Database.tx` is **re-entrant** (a depth counter; the outermost call does ``BEGIN IMMEDIATE`` / ``COMMIT`` /
  ``ROLLBACK``, a nested one is a ``SAVEPOINT`` so an inner failure that the caller catches rolls back only the
  inner part) and it **releases the lock when ``BEGIN`` itself fails**. The old ``_Transaction.__enter__`` took the
  lock and then ran ``BEGIN IMMEDIATE``; one ``database is locked`` error left the lock held forever and every
  other thread hung. That bug was in all 16 copies.
* ``busy_timeout`` is explicit (15 s by default) so a second process (the hub's online backup, Faustus reading)
  waits instead of failing with ``database is locked``.
* Migrations are a list of SQL strings or callables, applied in order, **each in its own transaction** together
  with the ``schema_version`` row. DDL runs statement by statement through the connection (``executescript``
  commits implicitly, so a failure half way used to leave partial DDL with the version not advanced).
* ``backup_to`` (online backup through the sqlite3 backup API), ``check_fts5``, settings with JSON values and the
  ``dumps`` / ``loads`` / ``row_dict`` helpers every ``db.py`` carried.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Optional, Sequence, Union

from .atomic import replace_with_retry, tmp_path_for

__all__ = ["Database", "Migration", "check_fts5", "split_statements", "dumps", "loads", "row_dict", "row_dicts"]

log = logging.getLogger("hoard_link.sqlkit")

Migration = Union[str, Callable[[sqlite3.Connection], None]]
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SYNC = {"OFF", "NORMAL", "FULL", "EXTRA", "0", "1", "2", "3"}
_JOURNAL = {"DELETE", "TRUNCATE", "PERSIST", "MEMORY", "WAL", "OFF"}


# ------------------------------------------------------------------ helpers

def dumps(obj: Any) -> str:
    """Compact JSON for a TEXT column (non-ASCII kept, unknown objects as ``str``)."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)


def loads(value: Any, default: Any = None) -> Any:
    """Parse a JSON TEXT column; ``None``, an empty string or invalid JSON give ``default``."""
    if value is None or value == "":
        return default
    if isinstance(value, (bytes, bytearray)):
        value = bytes(value).decode("utf-8", "replace")
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def row_dict(row: Optional[Mapping[str, Any]]) -> Optional[dict[str, Any]]:
    """``dict(row)`` that tolerates ``None``."""
    return None if row is None else dict(row)


def row_dicts(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


def split_statements(script: str) -> list[str]:
    """Split a SQL script into complete statements (``sqlite3.complete_statement`` understands quotes, comments and
    ``CREATE TRIGGER ... BEGIN ... END;`` bodies, so a ``;`` inside any of them does not split). A trailing fragment
    without ``;`` is kept as the last statement; comment-only fragments are dropped."""
    out: list[str] = []
    start = 0
    pos = script.find(";")
    while pos != -1:
        chunk = script[start:pos + 1]
        if sqlite3.complete_statement(chunk):
            if _has_sql(chunk):
                out.append(chunk.strip())
            start = pos + 1
        pos = script.find(";", pos + 1)
    tail = script[start:]
    if _has_sql(tail):
        out.append(tail.strip())
    return out


def _has_sql(fragment: str) -> bool:
    """False for whitespace and ``--`` / ``/* */`` comments only (``;`` alone counts as nothing too)."""
    text = re.sub(r"/\*.*?\*/", "", fragment, flags=re.S)
    return any(line.strip().strip(";").strip() and not line.strip().startswith("--") for line in text.splitlines())


def check_fts5(conn: Any) -> bool:
    """True when this SQLite build has FTS5 (a throwaway virtual table in the ``temp`` schema is created and
    dropped). Accepts a connection or a :class:`Database`."""
    raw = getattr(conn, "conn", conn)
    lock = getattr(conn, "lock", None)
    try:
        if lock is not None:
            lock.acquire()
        try:
            raw.execute("CREATE VIRTUAL TABLE IF NOT EXISTS temp.__hl_fts5_probe USING fts5(x)")
            raw.execute("DROP TABLE IF EXISTS temp.__hl_fts5_probe")
            return True
        finally:
            if lock is not None:
                lock.release()
    except sqlite3.Error:
        return False


def _ident(name: str) -> str:
    if not isinstance(name, str) or not _IDENT.match(name):
        raise ValueError(f"not a valid SQL identifier: {name!r}")
    return name


# ------------------------------------------------------------------ the database

class Database:
    """A SQLite file behind one shared connection and one re-entrant lock.

    ``migrations`` is a sequence of SQL strings (scripts of several statements are fine) or callables that receive
    the connection (already inside the migration's transaction). The 1-based position is the schema version, stored
    in ``schema_version``; existing databases keep working because only the entries after the stored version run.
    ``on_open(conn)`` runs after the pragmas and before the migrations (for ``secure_delete`` and the like).
    ``journal_mode`` overrides ``wal`` (Dorian keeps ``DELETE``).
    """

    def __init__(self, path: Union[str, Path], *, migrations: Sequence[Migration] = (), busy_timeout_ms: int = 15000,
                 wal: bool = True, foreign_keys: bool = True, synchronous: str = "NORMAL", journal_mode: Optional[str] = None,
                 check_same_thread: bool = False, on_open: Optional[Callable[[sqlite3.Connection], None]] = None):
        self.path = Path(path) if str(path) != ":memory:" else Path(":memory:")
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.migrations: list[Migration] = list(migrations)
        self.lock = threading.RLock()
        self._depth = 0
        self._sp = 0
        self._closed = False
        self._settings_ready = False
        sync = str(synchronous).upper()
        if sync not in _SYNC:
            raise ValueError(f"synchronous must be one of {sorted(_SYNC)}")
        mode = (journal_mode or ("WAL" if wal else "")).upper()
        if mode and mode not in _JOURNAL:
            raise ValueError(f"journal_mode must be one of {sorted(_JOURNAL)}")
        timeout_ms = max(0, int(busy_timeout_ms))
        self.conn = sqlite3.connect(str(path), timeout=timeout_ms / 1000.0, check_same_thread=check_same_thread, isolation_level=None)
        try:
            self.conn.row_factory = sqlite3.Row
            self.conn.execute(f"PRAGMA busy_timeout = {timeout_ms}")
            if mode and str(path) != ":memory:":
                self._set_journal_mode(mode, timeout_ms)
            self.conn.execute(f"PRAGMA synchronous = {sync}")
            self.conn.execute(f"PRAGMA foreign_keys = {'ON' if foreign_keys else 'OFF'}")
            if on_open is not None:
                on_open(self.conn)
            self.migrate()
        except BaseException:
            self.conn.close()
            raise

    def _set_journal_mode(self, mode: str, timeout_ms: int) -> None:
        """Switch the journal mode, retrying while another connection holds the file: SQLite does not run the busy
        handler for this pragma, so two processes opening a fresh database together got "database is locked"."""
        deadline = time.monotonic() + max(1.0, timeout_ms / 1000.0)
        delay = 0.01
        while True:
            try:
                row = self.conn.execute("PRAGMA journal_mode").fetchone()
                if row and str(row[0]).upper() == mode:
                    return
                self.conn.execute(f"PRAGMA journal_mode = {mode}")
                return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() and "busy" not in str(exc).lower() or time.monotonic() >= deadline:
                    raise
                time.sleep(delay)
                delay = min(0.2, delay * 2)

    # ------------------------------------------------------------ context manager
    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @property
    def closed(self) -> bool:
        return self._closed

    # ------------------------------------------------------------ schema
    @property
    def schema_version(self) -> int:
        """The highest migration applied (0 for a fresh file)."""
        with self.lock:
            return self._current_version()

    def _current_version(self) -> int:
        has = self.conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'").fetchone()
        if not has:
            return 0
        row = self.conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
        return int(row["v"] or 0)

    def migrate(self) -> int:
        """Apply the migrations after the stored version, each in its own ``BEGIN IMMEDIATE`` transaction together
        with its ``schema_version`` row; returns the version reached. Safe to call again and safe when two
        processes start together (the version is re-read inside the transaction)."""
        with self.lock:
            self.conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL, applied_at TEXT)")
            columns = {r["name"] for r in self.conn.execute("PRAGMA table_info(schema_version)")}
            current = self._current_version()
            if current > len(self.migrations):
                log.warning("%s is at schema version %s but this build only knows %s migrations", self.path, current, len(self.migrations))
            for index, step in enumerate(self.migrations, start=1):
                if index <= current:
                    continue
                with self.tx():
                    if self._current_version() >= index:     # another process got there first
                        continue
                    if callable(step):
                        step(self.conn)
                    else:
                        for statement in split_statements(step):
                            self.conn.execute(statement)
                    if "applied_at" in columns:
                        self.conn.execute("INSERT INTO schema_version(version, applied_at) VALUES (?, ?)",
                                          (index, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
                    else:
                        self.conn.execute("INSERT INTO schema_version(version) VALUES (?)", (index,))
            return self._current_version()

    # ------------------------------------------------------------ queries
    def query(self, sql: str, params: Union[Sequence[Any], Mapping[str, Any]] = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: Union[Sequence[Any], Mapping[str, Any]] = ()) -> Optional[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, params).fetchone()

    def scalar(self, sql: str, params: Union[Sequence[Any], Mapping[str, Any]] = (), default: Any = None) -> Any:
        """The first column of the first row (``default`` when there is no row or it is NULL)."""
        with self.lock:
            row = self.conn.execute(sql, params).fetchone()
        return default if row is None or row[0] is None else row[0]

    def execute(self, sql: str, params: Union[Sequence[Any], Mapping[str, Any]] = ()) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, params)

    def executemany(self, sql: str, seq: Iterable[Union[Sequence[Any], Mapping[str, Any]]]) -> sqlite3.Cursor:
        """All rows or none: runs inside :meth:`tx` (a savepoint when already in a transaction)."""
        with self.tx():
            return self.conn.executemany(sql, seq)

    def script(self, sql: str) -> None:
        """Run a multi-statement script statement by statement inside one transaction (never ``executescript``)."""
        with self.tx():
            for statement in split_statements(sql):
                self.conn.execute(statement)

    def insert(self, table: str, values: Mapping[str, Any], *, on_conflict: Optional[str] = None) -> int:
        """``INSERT INTO table(cols) VALUES (...)`` from a dict; returns ``lastrowid``. Table and column names are
        checked as identifiers. ``on_conflict`` is ``"replace"`` or ``"ignore"`` (``INSERT OR ...``)."""
        if not values:
            raise ValueError("insert needs at least one column")
        verb = "INSERT"
        if on_conflict:
            if on_conflict.lower() not in ("replace", "ignore"):
                raise ValueError("on_conflict must be 'replace' or 'ignore'")
            verb = f"INSERT OR {on_conflict.upper()}"
        cols = [_ident(c) for c in values]
        sql = f"{verb} INTO {_ident(table)}({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})"
        with self.lock:
            return int(self.conn.execute(sql, [values[c] for c in values]).lastrowid or 0)

    # ------------------------------------------------------------ transactions
    @contextmanager
    def tx(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        """``with db.tx() as conn:`` an atomic block, re-entrant.

        The outermost call runs ``BEGIN IMMEDIATE`` (``BEGIN`` with ``immediate=False``) and ``COMMIT``; an exception
        rolls back and propagates. A nested call inside the same thread joins it through a ``SAVEPOINT``. If
        ``BEGIN`` itself fails (for example ``database is locked`` past the busy timeout) the lock is released
        before the error propagates, so other threads keep working.
        """
        self.lock.acquire()
        try:
            outermost = self._depth == 0
            if outermost:
                self.conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
                savepoint = None
            else:
                self._sp += 1
                savepoint = f"hl_sp_{self._sp}"
                self.conn.execute(f"SAVEPOINT {savepoint}")
            self._depth += 1
        except BaseException:
            self.lock.release()
            raise
        try:
            try:
                yield self.conn
            except BaseException:
                try:
                    if savepoint is None:
                        self.conn.execute("ROLLBACK")
                    else:
                        self.conn.execute(f"ROLLBACK TO {savepoint}")
                        self.conn.execute(f"RELEASE {savepoint}")
                except sqlite3.Error:
                    log.exception("rollback failed")
                raise
            else:
                try:
                    self.conn.execute("COMMIT" if savepoint is None else f"RELEASE {savepoint}")
                except BaseException:
                    if savepoint is None:
                        try:
                            self.conn.execute("ROLLBACK")
                        except sqlite3.Error:
                            pass
                    raise
        finally:
            self._depth -= 1
            self.lock.release()

    #: the name the 16 old copies used (``with db.transaction():``)
    def transaction(self) -> Any:
        return self.tx()

    @property
    def in_transaction(self) -> bool:
        return self._depth > 0

    # ------------------------------------------------------------ settings
    def _ensure_settings(self) -> None:
        if not self._settings_ready:
            with self.lock:
                self.conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            self._settings_ready = True

    def get_setting(self, key: str, default: Any = None) -> Any:
        """The setting ``key`` (JSON decoded). A value stored as plain text by an older copy of this class that is
        not valid JSON comes back as that text."""
        self._ensure_settings()
        row = self.one("SELECT value FROM settings WHERE key = ?", (key,))
        if row is None:
            return default
        raw = row["value"]
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return raw

    def set_setting(self, key: str, value: Any) -> None:
        self._ensure_settings()
        self.execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     (key, dumps(value)))

    def delete_setting(self, key: str) -> None:
        self._ensure_settings()
        self.execute("DELETE FROM settings WHERE key = ?", (key,))

    # ------------------------------------------------------------ backup / close
    def backup_to(self, dest: Union[str, Path]) -> Path:
        """An online, consistent copy of the database at ``dest`` (the sqlite3 backup API; other processes and
        threads may keep using the file). Written to a temp file first and moved into place, so a failed backup
        never replaces a good one. Returns the destination path."""
        target = Path(dest)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = tmp_path_for(target)
        try:
            out = sqlite3.connect(str(tmp), isolation_level=None)
            try:
                with self.lock:
                    self.conn.backup(out)
            finally:
                out.close()
            replace_with_retry(tmp, target)
        except BaseException:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
        return target

    def close(self) -> None:
        """``wal_checkpoint(TRUNCATE)`` then close. Calling it twice is harmless."""
        with self.lock:
            if self._closed:
                return
            self._closed = True
            try:
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            self.conn.close()
