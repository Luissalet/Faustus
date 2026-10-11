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
(``UndoConflict``, naming the later change to undo first): undoing an old
edit over a newer one would silently lose the newer one.

The journal keeps text the stores themselves may have deleted, so it follows
what the person erased:

* wiping the memory store drops its rows (``wipe_store``) and records the
  wipe, so no surviving row can bring wiped data back;
* a definitive forget (the learned memory's tombstone) blanks the text of
  that item's rows (``scrub_item``); they stay as «forgotten» stubs that
  cannot be undone;
* «clear history» (``clear``) deletes the caller's rows;
* deletes use ``secure_delete`` and a VACUUM after big removals, so the text
  does not linger in free pages.

Retention is deterministic: rows older than ``KEEP_DAYS`` are pruned at
startup and at most once a day after that (the last run is stored in the
database). Stored text is capped per field (``MAX_FIELD_CHARS``): a longer
field keeps its first characters plus a length and hash marker, and a change
whose copy was cut cannot be undone.

Attribution comes from the running turn (``tool_execution`` turn options:
``run_id`` and ``turn_id``, whose prefix is the session id) or from
``attribution(...)`` for work that runs after the turn (the extractors).
Recording never raises and never blocks the write it describes.
"""

from __future__ import annotations

import contextlib
import hashlib
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
PRUNE_EVERY = 86400            # at most one retention pass a day
VACUUM_AFTER_ROWS = 200        # a removal this big also compacts the file
MAX_LIST = 500
MAX_FIELD_CHARS = 20_000       # per stored text field
MAX_ROW_CHARS = 100_000        # per stored before/after document
TRUNCATED_KEY = "_truncated"
_LABEL_CHARS = 160

#: Fields whose change counts as an edit (bookkeeping such as use counters,
#: access stamps or decayed confidence is not a memory change).
FACT_FIELDS = ("text", "category", "pinned")
INSTINCT_FIELDS = ("trigger", "action", "domain", "scope", "project")

_ATTR: ContextVar[Optional[Dict[str, str]]] = ContextVar("memory_journal_attr", default=None)
_SUPPRESS: ContextVar[bool] = ContextVar("memory_journal_suppress", default=False)
_LOCK = threading.Lock()


class UndoError(Exception):
    """The change cannot be undone. ``code`` is a stable machine-readable
    reason (Studio words it in the interface language); ``data`` carries the
    details that go with it."""

    code = "cannot_undo"

    def __init__(self, message: str, *, code: Optional[str] = None, **data: Any) -> None:
        super().__init__(message)
        if code:
            self.code = code
        self.data = data

    def payload(self) -> Dict[str, Any]:
        return {"code": self.code, "message": str(self), **self.data}


class UndoConflict(UndoError):
    """The item changed again after this change, or is gone."""

    code = "conflict"


class UndoGone(UndoError):
    """What this change would bring back was erased on purpose."""

    code = "gone"


def _now() -> float:
    return time.time()


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
    attr = dict(_ATTR.get() or {})
    if attr.get("turn_id") and not attr.get("session_id") and ":" in attr["turn_id"]:
        attr["session_id"] = attr["turn_id"].rsplit(":", 1)[0]
    out.update(attr)
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
    # Deleted text is zeroed in the file, not left in free pages.
    conn.execute("PRAGMA secure_delete = ON")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS changes (id TEXT PRIMARY KEY, ts REAL NOT NULL, owner TEXT NOT NULL, "
        "store TEXT NOT NULL, op TEXT NOT NULL, item_id TEXT NOT NULL, label TEXT, location TEXT, "
        "before TEXT, after TEXT, session_id TEXT, run_id TEXT, turn_id TEXT, source TEXT, "
        "undone_at REAL, undone_by TEXT, purged_at REAL)")
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(changes)")}
    if "purged_at" not in columns:
        conn.execute("ALTER TABLE changes ADD COLUMN purged_at REAL")
    conn.execute("CREATE INDEX IF NOT EXISTS changes_owner_ts ON changes(owner, ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS changes_session ON changes(session_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS changes_turn ON changes(turn_id)")
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS wipes (id INTEGER PRIMARY KEY AUTOINCREMENT, store TEXT NOT NULL, "
                 "ts REAL NOT NULL)")
    return conn


def _dump(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return None


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _clip(value: Any) -> Any:
    """Cap what one row stores. A text field over ``MAX_FIELD_CHARS`` keeps its
    first characters and a ``_truncated`` marker with its length and hash;
    anything still over ``MAX_ROW_CHARS`` as a whole is cut to short prefixes."""
    if not isinstance(value, Mapping):
        return value
    out: Dict[str, Any] = dict(value)
    marks: Dict[str, Any] = {}
    for key, item in value.items():
        if isinstance(item, str) and len(item) > MAX_FIELD_CHARS:
            out[key] = item[:MAX_FIELD_CHARS]
            marks[key] = {"chars": len(item), "sha256": _digest(item)}
    if marks:
        out[TRUNCATED_KEY] = marks
    dumped = _dump(out)
    if dumped is not None and len(dumped) > MAX_ROW_CHARS:
        for key, item in value.items():
            if isinstance(item, (int, float, bool)) or item is None:
                continue
            text = item if isinstance(item, str) else (_dump(item) or "")
            if len(text) > 500:
                out[key] = text[:500]
                marks[key] = {"chars": len(text), "sha256": _digest(text)}
        out[TRUNCATED_KEY] = marks
    return out


def _is_truncated(item: Any) -> bool:
    return isinstance(item, Mapping) and bool(item.get(TRUNCATED_KEY))


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


def _meta_get(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _meta_set(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))


def _vacuum(conn: sqlite3.Connection) -> None:
    with contextlib.suppress(sqlite3.Error):
        conn.commit()
        conn.execute("VACUUM")


def _prune_if_due(conn: sqlite3.Connection, now: float, *, force: bool = False) -> int:
    """One retention pass when none ran in the last day. The time of the last
    pass is stored in the database, so it does not depend on traffic."""
    try:
        last = float(_meta_get(conn, "last_prune") or 0)
    except ValueError:
        last = 0.0
    if not force and last and now - last < PRUNE_EVERY:
        return 0
    with conn:
        removed = conn.execute("DELETE FROM changes WHERE ts < ?", (now - KEEP_DAYS * 86400,)).rowcount
        conn.execute("DELETE FROM wipes WHERE ts < ?", (now - KEEP_DAYS * 86400,))
        _meta_set(conn, "last_prune", now)
    if removed >= VACUUM_AFTER_ROWS:
        _vacuum(conn)
    return max(0, removed)


def prune(*, force: bool = False) -> int:
    """Apply the retention window now (startup calls this). Returns the rows
    removed; 0 when one already ran today or there is no journal yet."""
    try:
        if not os.path.exists(db_path()):
            return 0
        with _LOCK, contextlib.closing(_connect()) as conn:
            return _prune_if_due(conn, _now(), force=force)
    except Exception as exc:  # noqa: BLE001 - housekeeping never breaks startup
        logger.debug("memory journal: prune skipped (%s)", exc)
        return 0


def record(store: str, op: str, item_id: Any, *, owner: Any, before: Any = None, after: Any = None,
           location: str = "", source: str = "") -> Optional[str]:
    """Write one change. Returns its id, or None when nothing was written."""
    if _SUPPRESS.get() or store not in STORES or op not in OPS or not item_id:
        return None
    try:
        ctx = _context()
        change_id = uuid.uuid4().hex
        now = _now()
        row = (change_id, now, str(owner or ""), store, op, str(item_id), _label(store, before, after),
               str(location or ""), _dump(_clip(before)), _dump(_clip(after)), ctx.get("session_id", ""),
               ctx.get("run_id", ""), ctx.get("turn_id", ""), ctx.get("source") or str(source or ""))
        with _LOCK, contextlib.closing(_connect()) as conn:
            with conn:
                conn.execute("INSERT INTO changes (id, ts, owner, store, op, item_id, label, location, before, after, "
                             "session_id, run_id, turn_id, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
            _prune_if_due(conn, now)
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


# -- reading ------------------------------------------------------------------------

def _row(row: sqlite3.Row, *, full: bool = False) -> Dict[str, Any]:
    out = {k: row[k] for k in ("id", "ts", "owner", "store", "op", "item_id", "label", "session_id",
                               "run_id", "turn_id", "source", "undone_at", "purged_at")}
    out["undone"] = row["undone_at"] is not None
    out["purged"] = row["purged_at"] is not None
    out["truncated"] = TRUNCATED_KEY in (row["before"] or "") or TRUNCATED_KEY in (row["after"] or "")
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


# -- erasing ------------------------------------------------------------------------

def clear(owner: Any) -> int:
    """Delete the caller's journal rows («clear history»). ``owner`` None
    clears every owner (single-user installs, like the list). The stores
    themselves are not touched. Returns the rows removed."""
    with _LOCK, contextlib.closing(_connect()) as conn:
        with conn:
            if owner is None:
                removed = conn.execute("DELETE FROM changes").rowcount
            else:
                removed = conn.execute("DELETE FROM changes WHERE owner = ?", (str(owner or ""),)).rowcount
        if removed:
            _vacuum(conn)
    return max(0, removed)


def wipe_store(store: str, *, location: str = "") -> int:
    """The person wiped a whole store: drop every row about it (all owners)
    and remember the wipe, so a row that somehow survives cannot be undone
    into data that was erased on purpose. ``location`` limits it to one
    memory file (rows without a location count too)."""
    if store not in STORES:
        return 0
    with _LOCK, contextlib.closing(_connect()) as conn:
        with conn:
            if location:
                removed = conn.execute("DELETE FROM changes WHERE store = ? AND (location = ? OR location = '')",
                                       (store, str(location))).rowcount
            else:
                removed = conn.execute("DELETE FROM changes WHERE store = ?", (store,)).rowcount
            conn.execute("INSERT INTO wipes (store, ts) VALUES (?, ?)", (store, _now()))
        _vacuum(conn)
    return max(0, removed)


def scrub_item(store: str, item_id: Any, *, owner: Any = None) -> int:
    """A definitive forget: blank the text every row about this item keeps
    (before, after and label). The rows stay as stubs that say it happened
    and cannot be undone. Returns the rows scrubbed."""
    if store not in STORES or not item_id:
        return 0
    try:
        sql = ("UPDATE changes SET label = '', before = NULL, after = NULL, location = '', purged_at = ? "
               "WHERE store = ? AND item_id = ? AND purged_at IS NULL")
        args: List[Any] = [_now(), store, str(item_id)]
        if owner is not None:
            sql += " AND owner = ?"
            args.append(str(owner or ""))
        with _LOCK, contextlib.closing(_connect()) as conn:
            with conn:
                scrubbed = conn.execute(sql, args).rowcount
            if scrubbed:
                _vacuum(conn)
        return max(0, scrubbed)
    except Exception as exc:  # noqa: BLE001 - the forget itself already happened
        logger.warning("memory journal: could not scrub %s/%s (%s)", store, item_id, exc)
        return 0


def _wiped_since(store: str, ts: float) -> bool:
    with contextlib.closing(_connect()) as conn:
        return conn.execute("SELECT 1 FROM wipes WHERE store = ? AND ts >= ? LIMIT 1", (store, ts)).fetchone() is not None


def _blocking(change: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """The newest not-undone change to the same item made after this one: the
    one to undo first."""
    with contextlib.closing(_connect()) as conn:
        row = conn.execute(
            "SELECT id, ts, op, label, store FROM changes WHERE owner = ? AND store = ? AND item_id = ? "
            "AND ts > ? AND id != ? AND undone_at IS NULL AND purged_at IS NULL ORDER BY ts DESC LIMIT 1",
            (change["owner"], change["store"], change["item_id"], change["ts"], change["id"])).fetchone()
    return {k: row[k] for k in ("id", "ts", "op", "label", "store")} if row else None


# -- undo --------------------------------------------------------------------------

def _same(current: Optional[Mapping[str, Any]], expected: Optional[Mapping[str, Any]], fields) -> bool:
    if current is None or expected is None:
        return current is None and expected is None
    return _pick(current, fields) == _pick(expected, fields)


def _norm(text: Any) -> str:
    return " ".join(str(text or "").split()).casefold()


def _duplicate(what: str, existing_id: Any) -> UndoConflict:
    return UndoConflict(f"The same {what} was added again under another id, so there is nothing to bring back.",
                        code="duplicate", existing_id=str(existing_id))


def _undo_facts(change: Dict[str, Any], memory_vector: Any) -> None:
    from src.memory import MemoryManager
    location = change.get("location") or ""
    if not location:
        raise UndoError("this change does not say which memory file it was in", code="no_location")
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
        twin = next((e for e in entries
                     if _norm(e.get("text")) == _norm(before.get("text"))
                     and (e.get("owner") or "") == (before.get("owner") or "")), None)
        if twin is not None:
            raise _duplicate("memory", twin.get("id"))
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
        key = engine._text_key(before.get("owner"), before.get("project"), before.get("level"), before.get("text"))
        twin = None
        with contextlib.suppress(Exception):
            for other in engine.list_items(owner=str(before.get("owner") or ""), project=str(before.get("project") or ""),
                                           level=str(before.get("level") or ""), limit=2000):
                if str(other.get("id")) != item_id and engine._text_key(
                        other.get("owner"), other.get("project"), other.get("level"), other.get("text")) == key:
                    twin = other.get("id")
                    break
        if twin is not None:
            raise _duplicate("learned memory", twin)
        engine.save_item(before)
        engine._index(item_id, str(before.get("text") or ""))
        with contextlib.suppress(Exception), engine._db() as conn:
            conn.execute(f"DELETE FROM {engine.TOMBSTONE_TABLE} WHERE item_id = ?", (item_id,))
    else:
        raise UndoError("learned-memory edits are recorded as delete + create; undo those", code="use_delete_create")


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
        twin = next((k for k, v in data.items() if isinstance(v, Mapping)
                     and _norm(v.get("trigger")) == _norm(before.get("trigger"))
                     and _norm(v.get("action")) == _norm(before.get("action"))), None)
        if twin is not None:
            raise _duplicate("instinct", twin)
        data[item_id] = dict(before)
    instincts._save_raw(owner, data)


def _undo_notes(change: Dict[str, Any]) -> None:
    from src.brain import db as bdb
    from src.brain import notes, vault
    owner = change.get("owner") or ""
    op = change["op"]
    before, after = change.get("before") or {}, change.get("after") or {}
    path = str(after.get("path") or before.get("path") or change["item_id"])
    if op == "delete":
        trash_id = after.get("trash_id")
        if not trash_id:
            raise UndoError("this delete did not go through the trash", code="no_trash")
        with bdb.db() as conn:
            row = conn.execute("SELECT content, path, restored_at FROM trash WHERE id = ? AND owner = ?",
                               (trash_id, owner)).fetchone()
        if row is None:
            raise UndoGone("the deleted note is no longer in the trash", code="trash_gone")
        if row["restored_at"]:
            raise UndoConflict("the note was already restored from the trash", code="changed_elsewhere")
        existing = vault._read(vault.abs_path(owner, vault.validate_path(row["path"])))
        if existing is not None and existing == row["content"]:
            raise _duplicate("note", row["path"])
        try:
            notes.restore(owner, trash_id)
        except ValueError as exc:
            raise UndoConflict(str(exc), code="changed_elsewhere") from exc
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
        raise UndoError("unknown change", code="unknown")
    if owner is not None and str(change.get("owner") or "") != str(owner or ""):
        raise UndoError("unknown change", code="unknown")
    if change["undone"]:
        raise UndoError("this change was already undone", code="already_undone")
    if change["purged"]:
        raise UndoGone("the text of this change was erased when the memory was forgotten, so it cannot be undone",
                       code="purged")
    if _wiped_since(change["store"], change["ts"]):
        raise UndoGone("this store was wiped after the change, so it cannot be undone", code="store_wiped")
    if _is_truncated(change.get("before")) or _is_truncated(change.get("after")):
        raise UndoError("only the start of this text was kept (it was very long), so it cannot be undone",
                        code="truncated")
    with suppressed():
        store = change["store"]
        try:
            if store == "facts":
                _undo_facts(change, memory_vector)
            elif store == "rules":
                _undo_rules(change)
            elif store == "instincts":
                _undo_instincts(change)
            elif store == "notes":
                _undo_notes(change)
            else:
                raise UndoError(f"unknown store {store!r}", code="unknown_store")
        except UndoConflict as exc:
            if exc.code != "conflict":
                raise
            later = _blocking(change)
            if later is None:
                raise UndoConflict(f"{exc} (outside the journal)", code="changed_elsewhere") from exc
            label = later["label"] or later["id"][:8]
            raise UndoConflict(f"Undo the later change first: {later['op']} “{label}”.",
                               code="later_change", blocking=later) from exc
    by = _context().get("source") or "user"
    with _LOCK, contextlib.closing(_connect()) as conn, conn:
        conn.execute("UPDATE changes SET undone_at = ?, undone_by = ? WHERE id = ?", (_now(), by, change_id))
    done = get_change(change_id) or change
    return {"ok": True, "change": {k: v for k, v in done.items() if k not in ("before", "after", "location")}}
