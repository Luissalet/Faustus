"""Journal of memory changes, with undo (Ágora #242, radar #478).

Every memory the assistant (or anything else) creates, edits or deletes in
the four stores Faustus keeps about the user is written here with the turn
that did it:

* ``facts``     - the personal facts store (``memory.json``, ``manage_memory``)
* ``rules``     - the learned memory engine (``memory_engine.db``)
* ``instincts`` - trigger -> action instincts (``instincts/<owner>/instincts.json``)
* ``notes``     - second-brain notes (the vault; deletes go to its trash)

Each row keeps the item as it was BEFORE and AFTER the change, so any change
can be undone on its own. An undo refuses when the item changed again since
(``UndoConflict``): undoing an old edit over a newer one would silently lose
the newer one.

Attribution comes from the running turn (``tool_execution`` turn options:
``run_id`` and ``turn_id``, whose prefix is the session id) or from
``attribution(...)`` for work that runs after the turn (the extractors).
Recording never raises and never blocks the write it describes.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from contextvars import ContextVar
from typing import Any, Dict, Iterator, List, Mapping, Optional

logger = logging.getLogger(__name__)

STORES = ("facts", "rules", "instincts", "notes")
OPS = ("create", "edit", "delete")
DB_FILENAME = "memory_journal.db"
KEEP_DAYS = 90
MAX_LIST = 500
_LABEL_CHARS = 160

#: Fields whose change counts as an edit (bookkeeping such as use counters,
#: access stamps or decayed confidence is not a memory change).
FACT_FIELDS = ("text", "category", "pinned")
INSTINCT_FIELDS = ("trigger", "action", "domain", "scope", "project")

_ATTR: ContextVar[Optional[Dict[str, str]]] = ContextVar("memory_journal_attr", default=None)
_SUPPRESS: ContextVar[bool] = ContextVar("memory_journal_suppress", default=False)
_LOCK = threading.Lock()


class UndoConflict(Exception):
    """The item changed again after this change, or is gone."""


class UndoError(Exception):
    """The change cannot be undone (unknown, someone else's, already undone)."""


# -- attribution ---------------------------------------------------------------

@contextlib.contextmanager
def attribution(*, session_id: Any = None, source: Any = None, turn_id: Any = None,
                run_id: Any = None) -> Iterator[None]:
    """Attribute the changes made inside this block (background extractors)."""
    token = _ATTR.set({k: str(v) for k, v in (("session_id", session_id), ("source", source),
                                              ("turn_id", turn_id), ("run_id", run_id)) if v})
    try:
        yield
    finally:
        _ATTR.reset(token)


@contextlib.contextmanager
def suppressed() -> Iterator[None]:
    """Writes inside this block are not journaled (an undo writing back)."""
    token = _SUPPRESS.set(True)
    try:
        yield
    finally:
        _SUPPRESS.reset(token)


def _context() -> Dict[str, str]:
    out: Dict[str, str] = {}
    try:
        from src.tool_execution import get_active_turn_options
        opts = get_active_turn_options() or {}
    except Exception:  # noqa: BLE001 - outside a turn there is nothing to read
        opts = {}
    turn_id = str(opts.get("turn_id") or "")
    if turn_id:
        out["turn_id"] = turn_id
        if ":" in turn_id:
            out["session_id"] = turn_id.rsplit(":", 1)[0]
    if opts.get("run_id"):
        out["run_id"] = str(opts["run_id"])
    out.update(_ATTR.get() or {})
    return out


# -- storage ---------------------------------------------------------------------

def db_path() -> str:
    from src import constants
    return os.path.join(constants.DATA_DIR, DB_FILENAME)


def _connect() -> sqlite3.Connection:
    path = db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE IF NOT EXISTS changes (id TEXT PRIMARY KEY, ts REAL NOT NULL, owner TEXT NOT NULL, "
        "store TEXT NOT NULL, op TEXT NOT NULL, item_id TEXT NOT NULL, label TEXT, location TEXT, "
        "before TEXT, after TEXT, session_id TEXT, run_id TEXT, turn_id TEXT, source TEXT, "
        "undone_at REAL, undone_by TEXT)")
    conn.execute("CREATE INDEX IF NOT EXISTS changes_owner_ts ON changes(owner, ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS changes_session ON changes(session_id)")
    return conn


def _dump(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return None


def _label(store: str, before: Any, after: Any) -> str:
    item = after if isinstance(after, Mapping) and after else before
    if not isinstance(item, Mapping):
        return ""
    if store == "instincts":
        text = f"{item.get('trigger') or ''} -> {item.get('action') or ''}"
    elif store == "notes":
        text = str(item.get("title") or item.get("path") or "")
    else:
        text = str(item.get("text") or "")
    text = " ".join(text.split())
    return text[:_LABEL_CHARS]


def record(store: str, op: str, item_id: Any, *, owner: Any, before: Any = None, after: Any = None,
           location: str = "", source: str = "") -> Optional[str]:
    """Write one change. Returns its id, or None when nothing was written."""
    if _SUPPRESS.get() or store not in STORES or op not in OPS or not item_id:
        return None
    try:
        ctx = _context()
        change_id = uuid.uuid4().hex
        row = (change_id, time.time(), str(owner or ""), store, op, str(item_id), _label(store, before, after),
               str(location or ""), _dump(before), _dump(after), ctx.get("session_id", ""),
               ctx.get("run_id", ""), ctx.get("turn_id", ""), ctx.get("source") or str(source or ""))
        with _LOCK, contextlib.closing(_connect()) as conn, conn:
            conn.execute("INSERT INTO changes (id, ts, owner, store, op, item_id, label, location, before, after, "
                         "session_id, run_id, turn_id, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
            if int(row[1]) % 97 == 0:
                conn.execute("DELETE FROM changes WHERE ts < ?", (row[1] - KEEP_DAYS * 86400,))
        return change_id
    except Exception as exc:  # noqa: BLE001 - a journal must never break a write
        logger.debug("memory journal: record skipped (%s)", exc)
        return None


def _pick(item: Mapping[str, Any], fields) -> Dict[str, Any]:
    return {f: item.get(f) for f in fields}


def record_diff(store: str, old: Mapping[str, Mapping[str, Any]], new: Mapping[str, Mapping[str, Any]], *,
                fields, owner_of=None, owner: Any = "", location: str = "") -> int:
    """Journal the difference between two ``{id: item}`` snapshots."""
    if _SUPPRESS.get():
        return 0
    count = 0
    try:
        def _owner(item: Mapping[str, Any]) -> Any:
            return owner_of(item) if owner_of else owner
        for item_id in new.keys() - old.keys():
            count += bool(record(store, "create", item_id, owner=_owner(new[item_id]), after=dict(new[item_id]),
                                 location=location, source=str(new[item_id].get("source") or "")))
        for item_id in old.keys() - new.keys():
            count += bool(record(store, "delete", item_id, owner=_owner(old[item_id]), before=dict(old[item_id]),
                                 location=location))
        for item_id in old.keys() & new.keys():
            if _pick(old[item_id], fields) != _pick(new[item_id], fields):
                count += bool(record(store, "edit", item_id, owner=_owner(new[item_id]),
                                     before=dict(old[item_id]), after=dict(new[item_id]), location=location))
    except Exception as exc:  # noqa: BLE001
        logger.debug("memory journal: diff skipped (%s)", exc)
    return count


def _row(row: sqlite3.Row, *, full: bool = False) -> Dict[str, Any]:
    out = {k: row[k] for k in ("id", "ts", "owner", "store", "op", "item_id", "label", "session_id",
                               "run_id", "turn_id", "source", "undone_at")}
    out["undone"] = row["undone_at"] is not None
    if full:
        out["before"] = json.loads(row["before"]) if row["before"] else None
        out["after"] = json.loads(row["after"]) if row["after"] else None
        out["location"] = row["location"]
    else:
        before = json.loads(row["before"]) if row["before"] else None
        out["before_label"] = _label(row["store"], before, None) if row["op"] == "edit" else ""
    return out


def list_changes(owner: Any, *, session_id: str = "", run_id: str = "", turn_id: str = "", store: str = "",
                 limit: Any = 100, include_undone: bool = True) -> List[Dict[str, Any]]:
    """Newest first. ``owner`` None lists every owner (single-user installs)."""
    try:
        limit = max(1, min(MAX_LIST, int(limit or 100)))
    except (TypeError, ValueError):
        limit = 100
    where, args = [], []
    if owner is not None:
        where.append("owner = ?")
        args.append(str(owner or ""))
    for column, value in (("session_id", session_id), ("run_id", run_id), ("turn_id", turn_id), ("store", store)):
        if value:
            where.append(f"{column} = ?")
            args.append(str(value))
    if not include_undone:
        where.append("undone_at IS NULL")
    sql = "SELECT * FROM changes" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY ts DESC LIMIT ?"
    try:
        with contextlib.closing(_connect()) as conn:
            rows = conn.execute(sql, (*args, limit)).fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.debug("memory journal: list failed (%s)", exc)
        return []
    return [_row(r) for r in rows]


def get_change(change_id: str) -> Optional[Dict[str, Any]]:
    with contextlib.closing(_connect()) as conn:
        row = conn.execute("SELECT * FROM changes WHERE id = ?", (str(change_id or ""),)).fetchone()
    return _row(row, full=True) if row else None


def for_turn(turn_id: str, *, limit: int = 20) -> List[Dict[str, Any]]:
    """Compact list for a turn summary."""
    if not turn_id:
        return []
    return [{k: c[k] for k in ("id", "store", "op", "label")}
            for c in list_changes(None, turn_id=turn_id, limit=limit)]


# -- undo --------------------------------------------------------------------------

def _same(current: Optional[Mapping[str, Any]], expected: Optional[Mapping[str, Any]], fields) -> bool:
    if current is None or expected is None:
        return current is None and expected is None
    return _pick(current, fields) == _pick(expected, fields)


def _undo_facts(change: Dict[str, Any], memory_vector: Any) -> None:
    from src.memory import MemoryManager
    location = change.get("location") or ""
    if not location:
        raise UndoError("this change does not say which memory file it was in")
    manager = MemoryManager(os.path.dirname(location), create_if_missing=False)
    manager.memory_file = location
    entries = manager.load_all_for_update()
    by_id = {str(e.get("id")): e for e in entries}
    item_id, op = change["item_id"], change["op"]
    before, after = change.get("before"), change.get("after")
    current = by_id.get(item_id)
    if op == "create":
        if not _same(current, after, FACT_FIELDS):
            raise UndoConflict("the memory changed or was removed after this change")
        entries = [e for e in entries if str(e.get("id")) != item_id]
    elif op == "edit":
        if not _same(current, after, FACT_FIELDS):
            raise UndoConflict("the memory changed again after this edit")
        for field in FACT_FIELDS:
            if field in before:
                current[field] = before[field]
            else:
                current.pop(field, None)
        current["timestamp"] = int(time.time())
    else:
        if current is not None:
            raise UndoConflict("a memory with this id exists again")
        entries.append(dict(before))
    manager.save(entries)
    if memory_vector is not None and getattr(memory_vector, "healthy", True):
        with contextlib.suppress(Exception):
            memory_vector.remove(item_id)
            if op != "create":
                memory_vector.add(item_id, str((before or {}).get("text") or ""))


def _undo_rules(change: Dict[str, Any]) -> None:
    from src import memory_engine as engine
    item_id, op = change["item_id"], change["op"]
    current = engine.get_item(item_id)
    if op == "create":
        if current is None:
            raise UndoConflict("the learned memory is already gone")
        if str(current.get("text") or "") != str((change.get("after") or {}).get("text") or ""):
            raise UndoConflict("the learned memory changed after this change")
        engine.delete_item(item_id)
    elif op == "delete":
        if current is not None:
            raise UndoConflict("a learned memory with this id exists again")
        before = dict(change.get("before") or {})
        engine.save_item(before)
        engine._index(item_id, str(before.get("text") or ""))
        with contextlib.suppress(Exception), engine._db() as conn:
            conn.execute(f"DELETE FROM {engine.TOMBSTONE_TABLE} WHERE item_id = ?", (item_id,))
    else:
        raise UndoError("learned-memory edits are recorded as delete + create; undo those")


def _undo_instincts(change: Dict[str, Any]) -> None:
    from src import instincts
    owner = change.get("owner") or None
    data = instincts._load_raw(owner)
    item_id, op = change["item_id"], change["op"]
    before, after = change.get("before"), change.get("after")
    current = data.get(item_id)
    if op == "create":
        if not _same(current, after, INSTINCT_FIELDS):
            raise UndoConflict("the instinct changed or was removed after this change")
        data.pop(item_id, None)
    elif op == "edit":
        if not _same(current, after, INSTINCT_FIELDS):
            raise UndoConflict("the instinct changed again after this edit")
        current.update({f: before.get(f) for f in INSTINCT_FIELDS})
    else:
        if current is not None:
            raise UndoConflict("an instinct with this id exists again")
        data[item_id] = dict(before)
    instincts._save_raw(owner, data)


def _undo_notes(change: Dict[str, Any]) -> None:
    from src.brain import notes, vault
    owner = change.get("owner") or ""
    op = change["op"]
    before, after = change.get("before") or {}, change.get("after") or {}
    path = str(after.get("path") or before.get("path") or change["item_id"])
    if op == "delete":
        trash_id = after.get("trash_id")
        if not trash_id:
            raise UndoError("this delete did not go through the trash")
        notes.restore(owner, trash_id)
        return
    current = vault._read(vault.abs_path(owner, vault.validate_path(path)))
    if current != after.get("content"):
        raise UndoConflict("the note changed after this change")
    if op == "create":
        notes.delete_note(owner, path)
    else:
        notes.write_note(owner, path, str(before.get("content") or ""))


def undo(change_id: str, owner: Any, *, memory_vector: Any = None) -> Dict[str, Any]:
    """Undo one change. ``owner`` None skips the ownership check (single user)."""
    change = get_change(change_id)
    if change is None:
        raise UndoError("unknown change")
    if owner is not None and str(change.get("owner") or "") != str(owner or ""):
        raise UndoError("unknown change")
    if change["undone"]:
        raise UndoError("this change was already undone")
    with suppressed():
        store = change["store"]
        if store == "facts":
            _undo_facts(change, memory_vector)
        elif store == "rules":
            _undo_rules(change)
        elif store == "instincts":
            _undo_instincts(change)
        elif store == "notes":
            _undo_notes(change)
        else:
            raise UndoError(f"unknown store {store!r}")
    by = _context().get("source") or "user"
    with _LOCK, contextlib.closing(_connect()) as conn, conn:
        conn.execute("UPDATE changes SET undone_at = ?, undone_by = ? WHERE id = ?", (time.time(), by, change_id))
    done = get_change(change_id) or change
    return {"ok": True, "change": {k: v for k, v in done.items() if k not in ("before", "after", "location")}}
