"""src/panel_db.py -- each user panel's own SQLite database (DATA_DIR/panels/<id>/panel.db).

The panel is the owner's app, so the database takes any SQL: tables, indexes, views, triggers,
PRAGMAs, attached files. Two limits keep the server responsive, nothing else:

* a statement that runs longer than ``timeout_s`` (default 10 s, at most 120 s) is interrupted;
* ``query`` returns at most ``limit`` rows (default 1000, at most 10000) and says ``more`` with the
  ``next_offset`` to ask for the next page.

``query`` returns rows as objects keyed by column name; ``exec`` runs one statement (with
``params``), the same statement for many parameter sets (``many``), a list of ``{sql, params}``
(``batch``) or a script with several statements, always in one transaction. BLOBs come back as
``{"base64": ...}``; dict and list parameters are stored as JSON text.
"""
from __future__ import annotations

import base64
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from src import panels

DEFAULT_LIMIT = 1000
MAX_LIMIT = 10_000
DEFAULT_TIMEOUT_S = 10.0
MAX_TIMEOUT_S = 120.0
DB_NAME = "panel.db"

_locks: Dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def _lock(panel_id: str) -> threading.RLock:
    with _locks_guard:
        return _locks.setdefault(panel_id, threading.RLock())


def path(panel_id: str) -> Path:
    return panels._existing(panel_id) / DB_NAME


def _timeout(value: Any) -> float:
    try:
        t = float(value) if value not in (None, "") else DEFAULT_TIMEOUT_S
    except (TypeError, ValueError):
        raise panels.PanelError("`timeout_s` must be a number of seconds", "panel.db_bad_args")
    return max(0.5, min(t, MAX_TIMEOUT_S))


def _connect(panel_id: str, timeout_s: float) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path(panel_id)), timeout=min(timeout_s, 30.0), isolation_level=None,
                           check_same_thread=False)
    conn.row_factory = None
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.DatabaseError:
        pass
    deadline = time.monotonic() + timeout_s
    # Interrupt anything that runs past the deadline (checked every ~20k VM steps).
    conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 20_000)
    return conn


def _param(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return int(value)
    return value


def _params(params: Any) -> Any:
    if params is None:
        return ()
    if isinstance(params, dict):
        return {str(k).lstrip(":@$"): _param(v) for k, v in params.items()}
    if isinstance(params, (list, tuple)):
        return tuple(_param(v) for v in params)
    return (_param(params),)


def _cell(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"base64": base64.b64encode(bytes(value)).decode("ascii")}
    return value


def _sql(sql: Any) -> str:
    if not isinstance(sql, str) or not sql.strip():
        raise panels.PanelError("`sql` is required", "panel.db_bad_args")
    return sql


def _db_error(exc: Exception) -> panels.PanelError:
    text = str(exc) or type(exc).__name__
    if text == "interrupted":
        return panels.PanelError("the statement ran past its time limit and was interrupted; raise timeout_s or narrow it",
                                 "panel.db_timeout")
    return panels.PanelError(f"SQLite: {text}", "panel.db_error")


def _limit(value: Any) -> int:
    if value in (None, ""):
        return DEFAULT_LIMIT
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise panels.PanelError("`limit` must be an integer", "panel.db_bad_args")
    return max(1, min(n, MAX_LIMIT))


def _offset(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        raise panels.PanelError("`offset` must be an integer", "panel.db_bad_args")


def query(panel_id: str, sql: Any, params: Any = None, *, limit: Any = None, offset: Any = 0,
          timeout_s: Any = None) -> Dict[str, Any]:
    """Run one statement and return its rows (a page of them). A statement that returns no rows
    (an INSERT, a CREATE) still runs and reports `changes`."""
    sql = _sql(sql)
    lim, off, tmo = _limit(limit), _offset(offset), _timeout(timeout_s)
    t0 = time.monotonic()
    with _lock(panel_id):
        conn = _connect(panel_id, tmo)
        try:
            try:
                cur = conn.execute(sql, _params(params))
            except sqlite3.ProgrammingError as exc:
                if "one statement at a time" in str(exc):
                    raise panels.PanelError("query runs one statement; use exec for a script", "panel.db_bad_args")
                raise _db_error(exc)
            except sqlite3.Error as exc:
                raise _db_error(exc)
            if cur.description is None:
                return {"columns": [], "rows": [], "row_count": 0, "offset": off, "limit": lim, "more": False,
                        "next_offset": None, "changes": conn.total_changes, "last_row_id": cur.lastrowid,
                        "ms": int((time.monotonic() - t0) * 1000)}
            cols = [d[0] for d in cur.description]
            rows: List[Dict[str, Any]] = []
            more = False
            try:
                skipped = 0
                while skipped < off:
                    chunk = cur.fetchmany(min(5000, off - skipped))
                    if not chunk:
                        break
                    skipped += len(chunk)
                while len(rows) < lim:
                    chunk = cur.fetchmany(min(1000, lim - len(rows)))
                    if not chunk:
                        break
                    rows.extend({c: _cell(v) for c, v in zip(cols, r)} for r in chunk)
                more = len(rows) == lim and cur.fetchone() is not None
            except sqlite3.Error as exc:
                raise _db_error(exc)
            return {"columns": cols, "rows": rows, "row_count": len(rows), "offset": off, "limit": lim, "more": more,
                    "next_offset": off + len(rows) if more else None, "ms": int((time.monotonic() - t0) * 1000)}
        finally:
            conn.close()


def _run_one(conn: sqlite3.Connection, sql: str, params: Any) -> Dict[str, Any]:
    before = conn.total_changes
    try:
        cur = conn.execute(sql, _params(params))
    except sqlite3.ProgrammingError as exc:
        if "one statement at a time" in str(exc) and params in (None, [], (), {}):
            _script(conn, sql)
            return {"changes": conn.total_changes - before, "last_row_id": None, "script": True}
        raise
    rows = None
    if cur.description is not None:
        cols = [d[0] for d in cur.description]
        rows = [{c: _cell(v) for c, v in zip(cols, r)} for r in cur.fetchmany(DEFAULT_LIMIT)]
    out = {"changes": conn.total_changes - before, "last_row_id": cur.lastrowid}
    if rows is not None:
        out["rows"] = rows
    return out


def _script(conn: sqlite3.Connection, sql: str) -> None:
    """Run several statements inside the open transaction (executescript would commit it)."""
    buf = ""
    for part in sql.split(";"):
        buf += part + ";"
        # complete_statement keeps a trigger's BEGIN ... END; together and ignores ';' inside strings.
        if sqlite3.complete_statement(buf):
            if buf.strip(" \t\r\n;"):
                conn.execute(buf)
            buf = ""
    if buf.strip(" \t\r\n;"):
        conn.execute(buf)


def exec_(panel_id: str, sql: Any = None, params: Any = None, *, many: Optional[Iterable[Any]] = None,
          batch: Optional[List[Dict[str, Any]]] = None, timeout_s: Any = None) -> Dict[str, Any]:
    """Change the database in one transaction: a statement, the same statement over `many`
    parameter sets, a `batch` of {sql, params}, or a script of several statements."""
    tmo = _timeout(timeout_s)
    if batch is None and many is None:
        sql = _sql(sql)
    if batch is not None:
        if not isinstance(batch, list) or not batch:
            raise panels.PanelError("`batch` must be a non-empty list of {sql, params}", "panel.db_bad_args")
        steps = []
        for i, item in enumerate(batch):
            if isinstance(item, str):
                item = {"sql": item}
            if not isinstance(item, dict):
                raise panels.PanelError(f"batch item {i} must be {{sql, params}}", "panel.db_bad_args")
            steps.append((_sql(item.get("sql")), item.get("params")))
    t0 = time.monotonic()
    with _lock(panel_id):
        conn = _connect(panel_id, tmo)
        try:
            try:
                conn.execute("BEGIN IMMEDIATE")
                if batch is not None:
                    results = [_run_one(conn, s, p) for s, p in steps]
                    out: Dict[str, Any] = {"changes": sum(r["changes"] for r in results), "results": results}
                elif many is not None:
                    sets = list(many)
                    before = conn.total_changes
                    conn.executemany(_sql(sql), [_params(p) for p in sets])
                    out = {"changes": conn.total_changes - before, "statements": len(sets)}
                else:
                    out = _run_one(conn, sql, params)
                conn.execute("COMMIT")
            except sqlite3.Error as exc:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise _db_error(exc)
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            out["ms"] = int((time.monotonic() - t0) * 1000)
            return out
        finally:
            conn.close()


def tables(panel_id: str) -> Dict[str, Any]:
    """Tables and views with their columns and row counts, and the file size."""
    p = path(panel_id)
    if not p.is_file():
        return {"tables": [], "bytes": 0}
    out = []
    with _lock(panel_id):
        conn = _connect(panel_id, DEFAULT_TIMEOUT_S)
        try:
            items = conn.execute("SELECT name, type, sql FROM sqlite_master WHERE type IN ('table','view') "
                                 "AND name NOT LIKE 'sqlite_%' ORDER BY type, name").fetchall()
            for name, kind, ddl in items:
                q = '"' + name.replace('"', '""') + '"'
                cols = [{"name": r[1], "type": r[2] or "", "notnull": bool(r[3]), "default": r[4], "pk": bool(r[5])}
                        for r in conn.execute(f"PRAGMA table_info({q})").fetchall()]
                try:
                    count = conn.execute(f"SELECT count(*) FROM {q}").fetchone()[0]
                except sqlite3.Error:
                    count = None
                out.append({"name": name, "type": kind, "columns": cols, "rows": count, "sql": ddl})
        except sqlite3.Error as exc:
            raise _db_error(exc)
        finally:
            conn.close()
    size = sum(f.stat().st_size for f in p.parent.glob(DB_NAME + "*") if f.is_file())
    return {"tables": out, "bytes": size}


def quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'
