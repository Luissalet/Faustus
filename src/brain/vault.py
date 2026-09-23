"""
brain/vault.py — the markdown vault on disk and its two-way sync with the
stores it mirrors (`memory_engine`, `memory.json`, entities from Lot B).

Why sync instead of a live view: the files are meant to be opened in any
editor and hand-edited — that is the whole point of a vault — so folding a
human's edit back into the store it mirrors has to be a deliberate,
auditable step, not a filesystem watcher racing someone's keystrokes.
`sync()` runs on demand (the UI's "Sync now") or on a timer
(`brain_vault_sync_seconds`), always import-then-export-then-reindex.

Only three kinds of note round-trip into a store: `mem:` (memory_engine),
`pmem:` (memory.json) and `ent:` (Lot B's entities — imported lazily, and
every entity feature degrades to "not shown" when Lot B is not installed).
Everything else the vault writes (Project/Objective/Concept/Home) is a
generated index: re-rendered every sync, its free-text "user zone" always
preserved. Free notes under Notes/ round-trip nowhere — the file itself is
the only copy, and sync just keeps it indexed.

The rules the sync lives by:

- The store is the truth for a mirrored note. Export renders memory,
  personal and entity notes from store data only, never from whatever the
  file happens to hold, so a change made in the store (a correction from
  chat, a refreshed entity summary) always reaches the file.
- A human edit is a minimal patch against the version this vault last
  exported (`vault_state.base_fm` / `base_zone`): only a field or text the
  human actually changed is applied, validated the same way the store
  validates its own writes, so a concurrent store change to another field
  is never reverted and an invalid value never reaches the store.
- Nothing is guessed. A file the sync cannot interpret safely (no generated
  marker, unreadable encoding, a source id of another owner, a copy of a
  mirrored note) is left untouched on disk and reported in `errors`; it is
  not overwritten until it can be read again.
- A file a human saves while a sync is running is never overwritten: every
  write first checks the file is still what the scan saw, and a changed
  file is simply imported on the next run.
- Moving or renaming a mirrored file in an editor is a move, not a
  deletion. A memory/personal note stays where the human put it; an entity
  file's new name is the entity's new name.
- A mirrored note whose source is gone from the store (forgotten,
  corrected elsewhere, suppressed, marked secret) leaves the vault — into
  the trash, content kept — unless the human edited it, in which case it
  stays and is reported.
- One owner's vault never reads, writes, suppresses or deletes a source
  that belongs to another owner, and the shared vault (owner "") only holds
  unowned data.
- A no-op sync is cheap: files whose (mtime, size) match what this vault
  last wrote or saw are not read at all, and a note whose render inputs did
  not change is not rendered at all.
"""

from __future__ import annotations

import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from . import db, frontmatter as fm, render

logger = logging.getLogger(__name__)

SCHEMA_NAME = "brain_vault"

db.register_schema(SCHEMA_NAME, [
    """CREATE TABLE IF NOT EXISTS notes (
        owner TEXT NOT NULL, path TEXT NOT NULL, title TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '',
        hash TEXT NOT NULL DEFAULT '', mtime REAL NOT NULL DEFAULT 0,
        size INTEGER NOT NULL DEFAULT 0, frontmatter TEXT NOT NULL DEFAULT '{}',
        tags TEXT NOT NULL DEFAULT '[]', updated_at TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (owner, path)
    )""",
    """CREATE TABLE IF NOT EXISTS links (
        owner TEXT NOT NULL, src_path TEXT NOT NULL, target TEXT NOT NULL,
        dst_path TEXT, label TEXT NOT NULL DEFAULT '',
        is_embed INTEGER NOT NULL DEFAULT 0
    )""",
    "CREATE INDEX IF NOT EXISTS brain_links_src ON links(owner, src_path)",
    "CREATE INDEX IF NOT EXISTS brain_links_dst ON links(owner, dst_path)",
    "CREATE INDEX IF NOT EXISTS brain_links_target ON links(owner, target)",
    """CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
        owner UNINDEXED, path UNINDEXED, title, body
    )""",
    """CREATE TABLE IF NOT EXISTS sync_state (
        owner TEXT NOT NULL, path TEXT NOT NULL, source TEXT NOT NULL DEFAULT '',
        rendered_hash TEXT NOT NULL DEFAULT '', file_hash TEXT NOT NULL DEFAULT '',
        synced_at TEXT NOT NULL DEFAULT '', PRIMARY KEY (owner, path)
    )""",
    """CREATE TABLE IF NOT EXISTS user_zones (
        owner TEXT NOT NULL, path TEXT NOT NULL, text TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL DEFAULT '', PRIMARY KEY (owner, path)
    )""",
    """CREATE TABLE IF NOT EXISTS trash (
        id TEXT PRIMARY KEY, owner TEXT NOT NULL, path TEXT NOT NULL,
        title TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '',
        content TEXT NOT NULL DEFAULT '', effect TEXT NOT NULL DEFAULT '',
        deleted_at TEXT NOT NULL DEFAULT '', restored_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS sync_runs (
        id TEXT PRIMARY KEY, owner TEXT NOT NULL, at TEXT NOT NULL,
        report TEXT NOT NULL DEFAULT '{}'
    )""",
    # Everything this vault exported, per path: the render fingerprint (skip
    # rendering an unchanged note), the file's (mtime, size) right after the
    # write (skip reading an unchanged file), and the exported frontmatter
    # and user zone — the BASE a human edit is diffed against.
    """CREATE TABLE IF NOT EXISTS vault_state (
        owner TEXT NOT NULL, path TEXT NOT NULL, source TEXT NOT NULL DEFAULT '',
        kind TEXT NOT NULL DEFAULT '', render_key TEXT NOT NULL DEFAULT '',
        file_hash TEXT NOT NULL DEFAULT '', mtime_ns INTEGER NOT NULL DEFAULT 0,
        size INTEGER NOT NULL DEFAULT -1, base_fm TEXT NOT NULL DEFAULT '{}',
        base_zone TEXT NOT NULL DEFAULT '', user_path INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL DEFAULT '', PRIMARY KEY (owner, path)
    )""",
    # Files under Memories/ the sync refused to turn into a memory, by
    # content hash — so the refusal is reported once, not on every run.
    """CREATE TABLE IF NOT EXISTS vault_refused (
        owner TEXT NOT NULL, path TEXT NOT NULL, hash TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (owner, path)
    )""",
])

MIRRORED_PREFIXES = ("mem:", "pmem:", "ent:")

#: Frontmatter fields of a memory note a human may edit.
MEM_EDITABLE = ("type", "level", "status", "confidence", "valid_from", "valid_until", "pinned")

#: Frontmatter keys a brand-new note under Memories/ may carry and still be
#: turned into a memory without losing anything the human wrote.
NEW_MEMORY_KEYS = frozenset({"kind", "type", "level", "tags", "created", "updated", "title"})

#: Markdown structure a single memory text cannot hold (it is one line).
_STRUCTURED_RE = re.compile(r"(?m)^[ \t]{0,3}(?:#{1,6}[ \t]|[-*+][ \t]|\d+[.)][ \t]|>|```|~~~|\|)")

_ID_SUFFIX_RE = re.compile(r"^(?P<stem>.*?) \((?P<id8>[0-9A-Za-z_-]{4,})\)(?: \d+)?$")


class VaultPathError(ValueError):
    """A note path this vault refuses: traversal, absolute, non-.md, .trash/."""


# ── paths ────────────────────────────────────────────────────────────────

def vault_root(owner: str = "") -> str:
    from src.settings import get_setting
    base = str(get_setting("brain_vault_dir") or "").strip() or os.path.join(db.brain_dir(), "vault")
    return os.path.join(base, db.owner_key(owner))


def validate_path(path: Any) -> str:
    """Normalize a vault-relative note path, or raise `VaultPathError`."""
    text = str(path or "").strip().replace("\\", "/")
    if not text:
        raise VaultPathError("empty path")
    if text.startswith("/") or (len(text) > 1 and text[1] == ":"):
        raise VaultPathError(f"absolute paths are not allowed: {path!r}")
    parts = [p for p in text.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        raise VaultPathError(f"path traversal is not allowed: {path!r}")
    if parts[0] == ".trash":
        raise VaultPathError(".trash is internal")
    if not parts[-1].lower().endswith(".md"):
        raise VaultPathError("only .md notes are allowed")
    return "/".join(parts)


def abs_path(owner: str, rel_path: str) -> str:
    rel = validate_path(rel_path)
    return os.path.join(vault_root(owner), *rel.split("/"))


def _iter_files(root: str):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".trash"]
        for name in filenames:
            if name.lower().endswith(".md"):
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, root).replace(os.sep, "/")
                yield rel, full


def _decode(raw: bytes) -> Tuple[str, bool]:
    """(text, clean): a leading byte-order mark dropped; bytes that are not
    UTF-8 replaced (clean=False) so an index or a preview still works."""
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    try:
        return raw.decode("utf-8"), True
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace"), False


def _read_checked(path: str) -> Tuple[Optional[str], bool]:
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
        return None, True
    return _decode(raw)


def _read(path: str) -> Optional[str]:
    """Lenient read (BOM dropped, undecodable bytes replaced) — for indexing
    and display. An import that writes to a store uses `_read_checked`."""
    return _read_checked(path)[0]


def _stat(path: str) -> Optional[Tuple[int, int]]:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


def _atomic_write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp{uuid.uuid4().hex[:8]}"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _title_from_path(rel_path: str) -> str:
    return os.path.splitext(os.path.basename(rel_path))[0]


# ── entities: optional (Lot B), imported lazily every time it's needed ──

def _entities():
    # importlib goes through sys.modules first, so a substituted module (a
    # test double, or a reload) is honoured even after the package attribute
    # was bound by an earlier import.
    try:
        import importlib
        return importlib.import_module("src.brain.entities")
    except ImportError:
        return None


def _entity_links_for(source_ref: str) -> List[Dict[str, Any]]:
    ent = _entities()
    if ent is None:
        return []
    try:
        return list(ent.mentions_for(source_ref) or [])
    except Exception:  # noqa: BLE001 - never block a render on Lot B trouble
        logger.debug("brain vault: mentions_for failed for %s", source_ref, exc_info=True)
        return []


def _personal_data_dir() -> str:
    from src import constants
    return constants.DATA_DIR


def _owns(record_owner: Any, owner: str) -> bool:
    return str(record_owner or "") == str(owner or "")


# ── small DB helpers ─────────────────────────────────────────────────────

def _all_sync_state(owner: str) -> Dict[str, Dict[str, Any]]:
    with db.db() as conn:
        rows = conn.execute("SELECT * FROM sync_state WHERE owner=?", (owner,)).fetchall()
    return {row["path"]: dict(row) for row in rows}


def _all_vault_state(owner: str) -> Dict[str, Dict[str, Any]]:
    with db.db() as conn:
        rows = conn.execute("SELECT * FROM vault_state WHERE owner=?", (owner,)).fetchall()
    return {row["path"]: dict(row) for row in rows}


def _db_delete_sync_state(owner: str, path: str) -> None:
    with db.db() as conn:
        conn.execute("DELETE FROM sync_state WHERE owner=? AND path=?", (owner, path))
        conn.execute("DELETE FROM vault_state WHERE owner=? AND path=?", (owner, path))


def _db_delete_notes_row(owner: str, path: str) -> None:
    with db.db() as conn:
        conn.execute("DELETE FROM notes WHERE owner=? AND path=?", (owner, path))
        conn.execute("DELETE FROM links WHERE owner=? AND src_path=?", (owner, path))
        conn.execute("DELETE FROM notes_fts WHERE owner=? AND path=?", (owner, path))
        conn.execute("DELETE FROM user_zones WHERE owner=? AND path=?", (owner, path))


def _get_user_zone_cache(owner: str, path: str) -> Optional[str]:
    with db.db() as conn:
        row = conn.execute(
            "SELECT text FROM user_zones WHERE owner=? AND path=?", (owner, path)
        ).fetchone()
    return row["text"] if row else None


def _trash_add(owner: str, path: str, title: str, source: str, content: str, effect: str) -> str:
    trash_id = uuid.uuid4().hex
    with db.db() as conn:
        conn.execute(
            "INSERT INTO trash (id, owner, path, title, source, content, effect, deleted_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (trash_id, owner, path, title, source, content, effect, db.now_iso()),
        )
    return trash_id


def _record_run(owner: str, report: Dict[str, Any]) -> None:
    with db.db() as conn:
        conn.execute(
            "INSERT INTO sync_runs (id, owner, at, report) VALUES (?,?,?,?)",
            (uuid.uuid4().hex, owner, report["at"], db.dumps(report)),
        )


def last_sync(owner: str = "") -> Optional[Dict[str, Any]]:
    owner = str(owner or "")
    with db.db() as conn:
        row = conn.execute(
            "SELECT report FROM sync_runs WHERE owner=? ORDER BY at DESC, rowid DESC LIMIT 1",
            (owner,),
        ).fetchone()
    return db.loads(row["report"], None) if row else None


def rekey_path(owner: str, old_path: str, new_path: str, *, user_path: bool = True) -> None:
    """A mirrored note moved from `old_path` to `new_path` (by a human in an
    editor, or by `notes.rename_note`): its state follows it, so the move is
    neither a deletion of the old path nor an unknown file at the new one."""
    with db.db() as conn:
        for table in ("sync_state", "vault_state"):
            conn.execute(f"DELETE FROM {table} WHERE owner=? AND path=?", (owner, new_path))
            conn.execute(f"UPDATE {table} SET path=? WHERE owner=? AND path=?", (new_path, owner, old_path))
        if user_path:
            conn.execute("UPDATE vault_state SET user_path=1 WHERE owner=? AND path=?", (owner, new_path))


# ── validating a human's frontmatter edit ────────────────────────────────

_TRUE_WORDS = {"true", "yes", "on", "1", "si", "sí"}
_FALSE_WORDS = {"false", "no", "off", "0", ""}


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    text = str(value).strip().lower()
    if text in _TRUE_WORDS:
        return True
    if text in _FALSE_WORDS:
        return False
    raise ValueError(f"{value!r} is not true/false")


def _as_iso(value: Any) -> str:
    """'' for an empty value (clears the field), an ISO UTC timestamp for a
    date/timestamp, ValueError for anything else."""
    if value is None or str(value).strip() == "":
        return ""
    parsed = db.parse_iso(value)
    if parsed is None:
        raise ValueError(f"{value!r} is not an ISO date or timestamp")
    from datetime import timezone
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _as_choice(value: Any, choices) -> str:
    text = str(value if value is not None else "").strip().lower()
    if text not in choices:
        raise ValueError(f"{value!r} is not one of {', '.join(choices)}")
    return text


def _validate_mem_field(key: str, value: Any) -> Any:
    from src import memory_engine as engine
    if key == "type":
        return _as_choice(value, engine.TYPES)
    if key == "level":
        return _as_choice(value, engine.LEVELS)
    if key == "status":
        return _as_choice(value, engine.STATUSES)
    if key == "confidence":
        if isinstance(value, bool):
            raise ValueError("not a number")
        try:
            number = float(str(value).strip().replace(",", "."))
        except (TypeError, ValueError):
            raise ValueError(f"{value!r} is not a number between 0 and 1") from None
        if not 0.0 <= number <= 1.0:
            raise ValueError(f"{value!r} is not between 0 and 1")
        return number
    if key in ("valid_from", "valid_until"):
        return _as_iso(value)
    if key == "pinned":
        return _as_bool(value)
    raise ValueError("not editable")


def _norm(key: str, value: Any) -> Any:
    """A comparable form of a frontmatter value, so reformatting a value
    (quotes, `Semantic` vs `semantic`, a date written differently) is not
    mistaken for a change, and a change is never missed."""
    if value is None:
        return ""
    if key in ("valid_from", "valid_until", "created", "updated"):
        try:
            return _as_iso(value)
        except ValueError:
            return str(value)
    if key == "pinned":
        try:
            return _as_bool(value)
        except ValueError:
            return str(value)
    if key == "confidence":
        try:
            return round(float(value), 6)
        except (TypeError, ValueError):
            return str(value)
    if key in ("aliases", "tags"):
        if isinstance(value, (list, tuple)):
            return [str(v).strip() for v in value if str(v).strip()]
        return [str(value).strip()] if str(value).strip() else []
    if isinstance(value, str):
        return value.strip().lower() if key in ("type", "level", "status") else value.strip()
    return value


def _same(key: str, a: Any, b: Any) -> bool:
    return _norm(key, a) == _norm(key, b)


def _same_text(a: Any, b: Any) -> bool:
    return " ".join(str(a or "").split()) == " ".join(str(b or "").split())


# ── one sync run's working state ─────────────────────────────────────────

class _Run:
    """What one sync (or one single-file import/export) knows and decides:
    the stat of every file at scan time, the vault's state rows (kept in
    memory and written back in one transaction at the end), and which paths
    and sources must not be touched this run."""

    def __init__(self, owner: str, report: Dict[str, Any], *, budget_s: float = 30.0) -> None:
        self.owner = str(owner or "")
        self.report = report
        self.start = time.time()
        self.budget_s = budget_s
        self.root = vault_root(self.owner)
        self.scan: Dict[str, Optional[Tuple[int, int]]] = {}
        self.sync_rows = _all_sync_state(self.owner)
        self.vstate = _all_vault_state(self.owner)
        self.dirty: Set[str] = set()
        self.hold_paths: Set[str] = set()
        self.hold_sources: Set[str] = set()
        self.imported: Set[str] = set()
        self.refused: Dict[str, str] = {}
        self.refused_dirty: Set[str] = set()
        self.import_complete = True
        self._refused_loaded = False
        self._by_source: Dict[str, Set[str]] = {}
        for rel in set(self.vstate) | set(self.sync_rows):
            self._reindex_path(rel, None)

    def _sources_at(self, rel: str) -> Set[str]:
        out = set()
        for table in (self.vstate, self.sync_rows):
            row = table.get(rel)
            if row and row.get("source"):
                out.add(str(row["source"]))
        return out

    def _reindex_path(self, rel: str, before: Optional[Set[str]]) -> None:
        for source in before or ():
            paths = self._by_source.get(source)
            if paths is not None:
                paths.discard(rel)
        for source in self._sources_at(rel):
            self._by_source.setdefault(source, set()).add(rel)

    # -- bookkeeping
    def over_budget(self) -> bool:
        return time.time() - self.start > self.budget_s

    def error(self, path: str, message: str) -> None:
        self.report["errors"].append(f"{path}: {message}")

    def hold(self, path: str, source: str = "") -> None:
        self.hold_paths.add(path)
        if source:
            self.hold_sources.add(source)

    def full(self, rel: str) -> str:
        return os.path.join(self.root, *rel.split("/"))

    def stat(self, rel: str) -> Optional[Tuple[int, int]]:
        """The stat this run first saw for `rel` (taken now if not yet)."""
        if rel not in self.scan:
            self.scan[rel] = _stat(self.full(rel))
        return self.scan[rel]

    def unchanged_since_scan(self, rel: str) -> bool:
        return _stat(self.full(rel)) == self.stat(rel)

    def paths_of(self, source: str) -> List[str]:
        return sorted(self._by_source.get(source, ()))

    def base(self, rel: str) -> Optional[Dict[str, Any]]:
        row = self.vstate.get(rel)
        if not row or not row.get("render_key"):
            return None
        return {"fm": db.loads(row.get("base_fm"), {}) or {}, "zone": row.get("base_zone") or "",
                "stem": _title_from_path(rel), "file_hash": row.get("file_hash") or ""}

    def recorded_hash(self, rel: str) -> str:
        row = self.vstate.get(rel) or self.sync_rows.get(rel) or {}
        return str(row.get("file_hash") or "")

    # -- state mutations (flushed in one transaction)
    def drop(self, rel: str) -> None:
        before = self._sources_at(rel)
        self.vstate.pop(rel, None)
        self.sync_rows.pop(rel, None)
        self._reindex_path(rel, before)
        self.dirty.add(rel)

    def rekey(self, old: str, new: str, *, user_path: bool) -> None:
        before_old, before_new = self._sources_at(old), self._sources_at(new)
        for table in (self.vstate, self.sync_rows):
            table.pop(new, None)
            if old in table:
                row = dict(table.pop(old))
                row["path"] = new
                table[new] = row
        if new in self.vstate:
            self.vstate[new]["user_path"] = 1 if user_path else 0
        self._reindex_path(old, before_old)
        self._reindex_path(new, before_new)
        self.dirty |= {old, new}

    def set_source(self, rel: str, source: str) -> None:
        """The note at `rel` now stands for `source` (a correction of a
        memory a human keeps at a path of their own)."""
        before = self._sources_at(rel)
        for table in (self.vstate, self.sync_rows):
            if rel in table:
                table[rel]["source"] = source
        self._reindex_path(rel, before)
        self.dirty.add(rel)

    def adopt(self, rel: str, source: str, text_hash: str, *, user_path: bool) -> None:
        """Start tracking a mirrored note this vault has no record of."""
        before = self._sources_at(rel)
        st = self.stat(rel) or (0, -1)
        self.vstate[rel] = {"owner": self.owner, "path": rel, "source": source, "kind": "",
                            "render_key": "", "file_hash": text_hash, "mtime_ns": st[0], "size": st[1],
                            "base_fm": "{}", "base_zone": "", "user_path": 1 if user_path else 0,
                            "updated_at": db.now_iso()}
        self.sync_rows[rel] = {"owner": self.owner, "path": rel, "source": source, "rendered_hash": "",
                               "file_hash": text_hash, "synced_at": db.now_iso()}
        self._reindex_path(rel, before)
        self.dirty.add(rel)

    def touch(self, rel: str, text_hash: str) -> None:
        """The file changed on disk but not in content: remember its new
        stat so it is not read again next time."""
        st = self.stat(rel)
        row = self.vstate.get(rel)
        if row is not None and st is not None and row.get("file_hash") == text_hash:
            row["mtime_ns"], row["size"] = st
            self.dirty.add(rel)

    def record(self, rel: str, *, source: str, kind: str, render_key: str, text: str,
               parts: render.Parts, mirrored: bool, user_path: bool) -> None:
        st = _stat(self.full(rel)) or (0, -1)
        self.scan[rel] = st
        now = db.now_iso()
        file_hash = db.sha(text)
        before = self._sources_at(rel)
        self.vstate[rel] = {
            "owner": self.owner, "path": rel, "source": source, "kind": kind,
            "render_key": render_key, "file_hash": file_hash, "mtime_ns": st[0], "size": st[1],
            "base_fm": db.dumps(parts[0]), "base_zone": parts[1], "user_path": 1 if user_path else 0,
            "updated_at": now,
        }
        if mirrored:
            idx = text.find(render.GENERATED_MARKER)
            self.sync_rows[rel] = {
                "owner": self.owner, "path": rel, "source": source,
                "rendered_hash": db.sha(text[idx:]) if idx >= 0 else "",
                "file_hash": file_hash, "synced_at": now,
            }
        else:
            self.sync_rows.pop(rel, None)
        self._reindex_path(rel, before)
        self.dirty.add(rel)

    def is_refused(self, rel: str, text_hash: str) -> bool:
        if not self._refused_loaded:
            with db.db() as conn:
                rows = conn.execute("SELECT path, hash FROM vault_refused WHERE owner=?",
                                    (self.owner,)).fetchall()
            self.refused = {r["path"]: r["hash"] for r in rows}
            self._refused_loaded = True
        return self.refused.get(rel) == text_hash

    def refuse(self, rel: str, text_hash: str) -> None:
        self.is_refused(rel, text_hash)
        self.refused[rel] = text_hash
        self.refused_dirty.add(rel)

    def flush(self) -> None:
        if not self.dirty and not self.refused_dirty:
            return
        with db.db() as conn:
            for rel in sorted(self.dirty):
                row = self.vstate.get(rel)
                if row is None:
                    conn.execute("DELETE FROM vault_state WHERE owner=? AND path=?", (self.owner, rel))
                else:
                    conn.execute(
                        "INSERT OR REPLACE INTO vault_state (owner, path, source, kind, render_key, "
                        "file_hash, mtime_ns, size, base_fm, base_zone, user_path, updated_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (self.owner, rel, row.get("source") or "", row.get("kind") or "",
                         row.get("render_key") or "", row.get("file_hash") or "",
                         int(row.get("mtime_ns") or 0), int(row.get("size") if row.get("size") is not None else -1),
                         row.get("base_fm") or "{}", row.get("base_zone") or "",
                         int(row.get("user_path") or 0), row.get("updated_at") or db.now_iso()),
                    )
                    if not str(row.get("source") or "").startswith(MIRRORED_PREFIXES):
                        conn.execute(
                            "INSERT INTO user_zones (owner, path, text, updated_at) VALUES (?,?,?,?) "
                            "ON CONFLICT(owner, path) DO UPDATE SET text=excluded.text, "
                            "updated_at=excluded.updated_at",
                            (self.owner, rel, row.get("base_zone") or "", db.now_iso()),
                        )
                srow = self.sync_rows.get(rel)
                if srow is None:
                    conn.execute("DELETE FROM sync_state WHERE owner=? AND path=?", (self.owner, rel))
                else:
                    conn.execute(
                        "INSERT OR REPLACE INTO sync_state (owner, path, source, rendered_hash, "
                        "file_hash, synced_at) VALUES (?,?,?,?,?,?)",
                        (self.owner, rel, srow.get("source") or "", srow.get("rendered_hash") or "",
                         srow.get("file_hash") or "", srow.get("synced_at") or db.now_iso()),
                    )
            for rel in sorted(self.refused_dirty):
                conn.execute("INSERT OR REPLACE INTO vault_refused (owner, path, hash) VALUES (?,?,?)",
                             (self.owner, rel, self.refused.get(rel, "")))
        self.dirty.clear()
        self.refused_dirty.clear()

    # -- files
    def retire(self, rel: str) -> bool:
        """Remove a file this vault wrote and no longer wants at `rel` (the
        note moved or its source is gone) — only when it is exactly what the
        vault last wrote, or what this run just imported."""
        if rel in self.hold_paths:
            return False
        if self.stat(rel) is None:
            self.drop(rel)
            return True
        if not self.unchanged_since_scan(rel):
            self.report["notes"].append(f"{rel}: changed during the sync, left for the next run")
            self.hold(rel)
            return False
        if rel not in self.imported:
            text = _read(self.full(rel))
            if text is not None and db.sha(text) != self.recorded_hash(rel):
                self.error(rel, "edited since the last sync and no longer needed here; left in place")
                self.hold(rel)
                return False
        try:
            os.remove(self.full(rel))
        except OSError as exc:
            self.error(rel, f"could not remove ({exc})")
            return False
        self.scan[rel] = None
        self.drop(rel)
        return True


# ── the plan: every note the stores should produce, with unique names ───

@dataclass
class _Target:
    source: str
    kind: str
    folder: str
    stem: str
    idkey: str
    priority: int
    order: Tuple[Any, ...]
    parts: Callable[[str, Dict[str, str]], render.Parts]
    mirrored: bool = False
    # A cheap stand-in for `parts` when computing those is expensive (an
    # entity profile costs several store queries): same inputs -> same
    # fingerprint, and an unchanged fingerprint skips the render entirely.
    fingerprint: Optional[Callable[[Dict[str, str]], str]] = None


@dataclass
class _Plan:
    targets: Dict[str, _Target] = field(default_factory=dict)
    paths: Dict[str, str] = field(default_factory=dict)
    titles: Dict[str, str] = field(default_factory=dict)
    # which mirrored categories were listed completely (a partial listing
    # must never be read as "everything else is gone")
    complete: Dict[str, bool] = field(default_factory=dict)


def _memory_listing(owner: str) -> Tuple[List[Dict[str, Any]], bool]:
    from src import memory_engine as engine
    items = engine.list_items(owner=owner, limit=2000)
    live = [
        m for m in items
        if not m.get("suppressed") and m.get("sensitivity") != "secret"
        and m.get("status") in ("active", "deprecated", "anti_pattern")
    ]
    return live, len(items) < 2000


def _personal_listing(owner: str) -> Tuple[List[Dict[str, Any]], bool]:
    """This owner's personal memories only — the shared vault (owner "")
    holds the unowned ones, never everybody's."""
    from src import memory as memmod
    mgr = memmod.MemoryManager(_personal_data_dir())
    try:
        entries = mgr.load_all_for_update()
    except memmod.MemoryStoreUnreadable:
        return [], False
    return [e for e in entries if _owns(e.get("owner"), owner) and e.get("id")], True


def _personal_entry(owner: str, entry_id: str) -> Optional[Dict[str, Any]]:
    entries, _ = _personal_listing(owner)
    return next((e for e in entries if str(e.get("id")) == entry_id), None)


def _mentions_by_source(owner: str, entities_by_id: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, List[Dict[str, Any]]]]:
    """source_ref -> mentioned entities, in ONE query when the real entities
    module (and its `mentions` table) is installed; None otherwise, and the
    caller asks `mentions_for` per note instead."""
    ent = _entities()
    if ent is None or not getattr(ent, "__file__", None) or not hasattr(ent, "mentions_for"):
        return None
    try:
        with db.db() as conn:
            rows = conn.execute(
                "SELECT source_ref, entity_id FROM mentions WHERE owner=? ORDER BY rowid", (owner,)
            ).fetchall()
    except Exception:  # noqa: BLE001 - schema not there: fall back to per-note lookups
        return None
    out: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        entity = entities_by_id.get(row["entity_id"])
        if entity is not None:
            out.setdefault(row["source_ref"], []).append(entity)
    return out


def _norm_workspace(path: Any) -> str:
    text = str(path or "").strip()
    if not text:
        return ""
    return os.path.normcase(os.path.normpath(text)).rstrip("\\/")


def _project_resolver(projects: List[Dict[str, Any]]) -> Callable[[Any], str]:
    """memory `project` -> project id. Memories written from a chat carry
    the WORKSPACE PATH as their project, others the project id; both land in
    the same project note."""
    by_key: Dict[str, str] = {}
    for p in projects:
        pid = str(p.get("id") or "")
        if not pid:
            continue
        by_key.setdefault(pid, pid)
        ws = _norm_workspace(p.get("workspace") or p.get("path"))
        if ws:
            by_key.setdefault(ws, pid)

    def resolve(value: Any) -> str:
        text = str(value or "")
        if not text:
            return ""
        return by_key.get(text) or by_key.get(_norm_workspace(text)) or ""

    return resolve


# Overridable in tests (each wraps a store this lot does not own) — kept as
# module functions rather than inlined so a test can monkeypatch just the
# data source without touching real projects/objectives/concepts storage.

def _projects_for_owner(owner: str) -> List[Dict[str, Any]]:
    try:
        from services import projects as projects_mod
        return projects_mod.get_store().list(owner=owner)
    except Exception:  # noqa: BLE001
        logger.debug("brain vault: could not list projects", exc_info=True)
        return []


def _objectives_for_project(project: Dict[str, Any]) -> List[Dict[str, Any]]:
    try:
        from services import objectives as objectives_mod
        state = objectives_mod.load_state(project)
        return list((state.get("objectives") or {}).values())
    except Exception:  # noqa: BLE001
        logger.debug("brain vault: could not load objectives", exc_info=True)
        return []


def _concepts_for_project(project: Dict[str, Any]) -> List[Dict[str, Any]]:
    try:
        from src import project_concepts
        key = project_concepts.resolve_project_key(project_id=project.get("id"))
        return project_concepts.Store(key).all_concepts()
    except Exception:  # noqa: BLE001
        logger.debug("brain vault: could not load concepts", exc_info=True)
        return []


def _latest_daily_dates(owner: str, limit: int = 10) -> List[str]:
    root = os.path.join(vault_root(owner), "Daily")
    if not os.path.isdir(root):
        return []
    names = [name[:-3] for name in os.listdir(root) if name.lower().endswith(".md")]
    return sorted(names, reverse=True)[:limit]


def _entity_profile(entity: Dict[str, Any]) -> Dict[str, Any]:
    ent = _entities()
    if ent is None:
        return {}
    try:
        return ent.profile(entity["id"]) or {}
    except Exception:  # noqa: BLE001
        return {}


def _entity_fingerprint(entity: Dict[str, Any], items_by_id: Dict[str, Dict[str, Any]],
                        entries_by_id: Dict[str, Dict[str, Any]], entities_by_id: Dict[str, Dict[str, Any]],
                        titles: Dict[str, str], hour: str) -> str:
    """Everything an entity note is rendered from, without building the
    profile: the entity row, the text and window of every source that
    mentions it, its current relations and the names/notes of the entities
    they point at. The hour is part of it, so validity windows that close as
    time passes (and anything this misses) refresh within the hour."""
    facts = []
    for ref in sorted(str(r) for r in entity.get("mentions") or []):
        src = items_by_id.get(ref[4:]) if ref.startswith("mem:") else (
            entries_by_id.get(ref[5:]) if ref.startswith("pmem:") else None)
        facts.append([ref, {k: (src or {}).get(k) for k in (
            "text", "valid_from", "valid_until", "created_at", "updated_at", "timestamp")}])
    others = set()
    for rel in entity.get("relations") or []:
        for key in ("src", "dst"):
            if rel.get(key):
                others.add(str(rel[key]))
    names = [[oid, (entities_by_id.get(oid) or {}).get("name"), titles.get(f"ent:{oid}")]
             for oid in sorted(others)]
    row = {k: v for k, v in entity.items() if k not in ("mentions", "relations")}
    return db.sha(db.dumps([row, facts, entity.get("relations"), names, hour]))


def _build_plan(run: _Run) -> _Plan:
    """Every note the stores currently call for, each with its final path:
    unique basenames across the whole vault (case-insensitive file systems fold
    case, and a
    `[[link]]` resolves by basename), assigned deterministically — Home,
    then projects, entities, objectives, concepts, personal notes and
    memories; a later one whose name is taken gets ` (<id8>)`. A mirrored
    note a human moved keeps the path they chose."""
    owner = run.owner
    plan = _Plan()

    items, plan.complete["mem:"] = _memory_listing(owner)
    entries, plan.complete["pmem:"] = _personal_listing(owner)
    ent = _entities()
    entities: List[Dict[str, Any]] = []
    plan.complete["ent:"] = False
    if ent is not None:
        try:
            entities = [e for e in ent.list_entities(owner=owner, limit=2000)
                        if not e.get("hidden") and _owns(e.get("owner", owner), owner)]
            plan.complete["ent:"] = len(entities) < 2000
        except Exception as exc:  # noqa: BLE001
            logger.exception("brain vault: listing entities failed")
            run.report["errors"].append(f"export entities: {exc}")
    entities_by_id = {str(e["id"]): e for e in entities}
    mentions = _mentions_by_source(owner, entities_by_id)
    try:
        projects = list(_projects_for_owner(owner) or [])
    except Exception as exc:  # noqa: BLE001
        run.report["errors"].append(f"export projects: {exc}")
        projects = []
    resolve_project = _project_resolver(projects)

    def add(target: _Target) -> None:
        plan.targets[target.source] = target

    # Home
    latest_memories = items[:20]
    counts: Dict[str, int] = {}
    for e in entities:
        etype = str(e.get("type") or "other")
        counts[etype] = counts.get(etype, 0) + 1
    entity_counts = [{"type": t, "count": c} for t, c in sorted(counts.items())]
    latest_daily = _latest_daily_dates(owner)

    def home_parts(uz, titles, projects=projects):
        return render.home_parts(projects=projects, entity_counts=entity_counts,
                                 latest_memories=latest_memories, latest_daily=latest_daily,
                                 user_zone=uz, titles=titles)

    add(_Target("home", "home", "", "Home", "home", 0, ("",), home_parts))

    # Projects, their objectives and concepts
    memories_by_project: Dict[str, List[Dict[str, Any]]] = {}
    for m in items:
        pid = resolve_project(m.get("project"))
        if pid:
            memories_by_project.setdefault(pid, []).append(m)
    for project in projects:
        pid = str(project.get("id") or "")
        if not pid:
            continue
        pname = str(project.get("name") or pid)
        objectives = [o for o in _objectives_for_project(project) if o.get("status") != "dropped"]
        concepts = _concepts_for_project(project)
        project_entities = [e for e in entities if str(e.get("project") or "") == pid]
        project_memories = memories_by_project.get(pid, [])
        psource = f"proj:{pid}"

        def project_parts(uz, titles, project=project, memories=project_memories, objectives=objectives,
                          concepts=concepts, project_entities=project_entities, pname=pname):
            return render.project_note_parts(project, memories=memories, objectives=objectives,
                                             concepts=concepts, entities=project_entities,
                                             user_zone=uz, titles=titles, project_name=pname)

        add(_Target(psource, "project", "Projects", db.safe_filename(pname), pid, 1,
                    (str(project.get("created_at") or ""), pid), project_parts))
        for obj in objectives:
            oid = str(obj.get("id") or "")

            def obj_parts(uz, titles, obj=obj, pname=pname, psource=psource):
                return render.objective_note_parts(obj, pname, user_zone=uz,
                                                   project_title=titles.get(psource, pname))

            add(_Target(f"obj:{pname}/{oid}", "objective", f"Objectives/{db.safe_filename(pname)}",
                        db.safe_filename(str(obj.get("title") or oid)), oid, 3,
                        (pid, str(obj.get("created_at") or ""), oid), obj_parts))
        for concept in concepts:
            cid = str(concept.get("id") or "")

            def concept_parts(uz, titles, concept=concept, pname=pname, psource=psource):
                return render.concept_note_parts(concept, pname, user_zone=uz,
                                                 project_title=titles.get(psource, pname))

            add(_Target(f"concept:{pname}/{cid}", "concept", f"Concepts/{db.safe_filename(pname)}",
                        db.safe_filename(str(concept.get("name") or cid)), cid, 4,
                        (pid, str(concept.get("created_at") or ""), cid), concept_parts))

    # Entities
    items_by_id = {str(m["id"]): m for m in items}
    entries_by_id = {str(x.get("id")): x for x in entries}
    hour = time.strftime("%Y%m%d%H", time.gmtime())
    for e in entities:
        eid = str(e["id"])

        def entity_parts(uz, titles, e=e):
            return render.entity_note_parts(e, _entity_profile(e), user_zone="", titles=titles)

        entity_fp = None
        if isinstance(e.get("mentions"), list) and isinstance(e.get("relations"), list):
            def entity_fp(titles, e=e):
                return _entity_fingerprint(e, items_by_id, entries_by_id, entities_by_id, titles, hour)

        stem = db.safe_filename(e.get("name", ""))
        # an entity whose name IS its file name ("Orca") keeps the plain name
        # before one whose name had to be cleaned into it ("Orca#")
        add(_Target(f"ent:{eid}", "entity", f"Entities/{str(e.get('type') or 'other').capitalize()}",
                    stem, eid, 2, (0 if stem == e.get("name") else 1, str(e.get("created_at") or ""), eid),
                    entity_parts, mirrored=True, fingerprint=entity_fp))

    # Personal notes
    for entry in entries:
        pid_ = str(entry.get("id"))

        def personal_parts(uz, titles, entry=entry):
            return render.personal_note_parts(entry, user_zone="")

        add(_Target(f"pmem:{pid_}", "personal", "Personal", render.personal_note_title(entry), pid_, 5,
                    (str(entry.get("timestamp") or ""), pid_), personal_parts, mirrored=True))

    # Memories
    for item in items:
        mid = str(item["id"])
        source = f"mem:{mid}"

        def memory_parts(uz, titles, item=item, source=source):
            linked = mentions.get(source, []) if mentions is not None else _entity_links_for(source)
            pid = resolve_project(item.get("project"))
            return render.memory_note_parts(item, entities=linked, user_zone="", titles=titles,
                                            project_title=titles.get(f"proj:{pid}", "") if pid else "")

        add(_Target(source, "memory", f"Memories/{render.TYPE_FOLDERS.get(item.get('type'), 'Facts')}",
                    render.memory_note_title(item), mid, 6, (str(item.get("created_at") or ""), mid),
                    memory_parts, mirrored=True))

    # Paths: human-placed first, then by priority with a suffix on collision.
    taken: Dict[str, str] = {}
    for rel, row in sorted(run.vstate.items()):
        source = str(row.get("source") or "")
        if (row.get("user_path") and source in plan.targets and source not in plan.paths
                and run.stat(rel) is not None):
            plan.paths[source] = rel
            taken.setdefault(_title_from_path(rel).casefold(), source)
    for target in sorted(plan.targets.values(), key=lambda t: (t.priority, t.order)):
        if target.source in plan.paths:
            continue
        stem = target.stem or "untitled"
        key = stem.casefold()
        if key in taken and taken[key] != target.source:
            base = f"{stem} ({target.idkey[:8]})"
            stem, n = base, 2
            while stem.casefold() in taken:
                stem = f"{base} {n}"
                n += 1
        taken[stem.casefold()] = target.source
        plan.paths[target.source] = f"{target.folder}/{stem}.md" if target.folder else f"{stem}.md"
    plan.titles = {source: _title_from_path(path) for source, path in plan.paths.items()}
    return plan


# ── export ───────────────────────────────────────────────────────────────

def _owned_by_us(run: _Run, rel: str, target: _Target, text: Optional[str]) -> bool:
    """May the export write over the file at `rel`? Yes when this vault
    wrote it (state row), or when the file already says it is this note (a
    vault from before the state table, or rebuilt after losing brain.db)."""
    if rel in run.vstate or rel in run.sync_rows or text is None:
        return True
    data, _ = fm.split(text)
    if str(data.get("source") or "") == target.source:
        return True
    return target.source == "home" and data.get("kind") == "home"


def _export_target(run: _Run, plan: _Plan, target: _Target) -> Optional[str]:
    rel = plan.paths[target.source]
    if rel in run.hold_paths or target.source in run.hold_sources:
        return None
    full = run.full(rel)
    seen = run.stat(rel)
    if _stat(full) != seen:
        run.report["notes"].append(f"{rel}: changed during the sync, left for the next run")
        run.hold(rel, target.source)
        return None
    row = run.vstate.get(rel)
    state_fresh = bool(row) and seen is not None and (row.get("mtime_ns"), row.get("size")) == seen \
        and row.get("source") == target.source
    existing: Optional[str] = None
    loaded = False

    def load() -> Optional[str]:
        nonlocal existing, loaded
        if not loaded:
            existing = _read(full) if seen is not None else None
            loaded = True
        return existing

    if target.mirrored:
        zone = ""  # rendered from the store, never from the file
    elif seen is None:
        cached = row.get("base_zone") if row else _get_user_zone_cache(run.owner, rel)
        zone = cached or ""
    elif state_fresh:
        zone = row.get("base_zone") or ""
    else:
        text = load()
        _, body = fm.split(text or "")
        if text and not render.has_marker(body) and rel in run.vstate:
            run.error(rel, "the generated-sections marker line is missing; not re-rendered, file left as is")
            run.hold(rel, target.source)
            return None
        zone = render.user_zone_of(body)

    if target.fingerprint is not None:
        parts = None
        render_key = db.sha(target.kind + "\x00fp\x00" + target.fingerprint(plan.titles))
    else:
        parts = target.parts(zone, plan.titles)
        render_key = db.sha(target.kind + "\x00" + db.dumps(list(parts)))
    user_path = bool(row and row.get("user_path") and row.get("source") == target.source)
    if not (state_fresh and row.get("render_key") == render_key):
        if parts is None:
            parts = target.parts(zone, plan.titles)
        text = render.compose(*parts)
        current = load()
        if current is not None and not _owned_by_us(run, rel, target, current):
            run.error(rel, f"a note that is not this vault's copy of {target.source} is in the way; not overwritten")
            run.hold(rel, target.source)
            return None
        if current != text:
            _atomic_write(full, text)
            run.report["exported"] += 1
        run.record(rel, source=target.source, kind=target.kind, render_key=render_key, text=text,
                   parts=parts, mirrored=target.mirrored, user_path=user_path)
    for other in run.paths_of(target.source):
        if other != rel:
            run.retire(other)
    return rel


def _export_all(run: _Run, plan: _Plan, skip_sources: Set[str]) -> None:
    for target in sorted(plan.targets.values(), key=lambda t: (t.priority, t.order)):
        if run.over_budget():
            run.report["notes"].append("export phase stopped early: time budget exceeded")
            break
        if target.source in skip_sources:
            continue
        try:
            _export_target(run, plan, target)
        except Exception as exc:  # noqa: BLE001 - one bad note must not sink the run
            logger.exception("brain vault: export failed for %s", target.source)
            run.error(plan.paths.get(target.source, target.source), f"export failed ({exc})")


def _retire_gone_sources(run: _Run, plan: _Plan) -> None:
    """A mirrored note whose source is no longer exported (forgotten,
    corrected elsewhere, suppressed, made secret, hidden) moves to the
    trash — content kept — unless a human edited it since the last sync."""
    moved = 0
    for rel, row in sorted(run.sync_rows.items()):
        source = str(row.get("source") or "")
        prefix = next((p for p in MIRRORED_PREFIXES if source.startswith(p)), None)
        if prefix is None or not plan.complete.get(prefix) or source in plan.targets:
            continue
        if rel in run.hold_paths or run.stat(rel) is None:
            continue
        if not run.unchanged_since_scan(rel):
            continue
        text = _read(run.full(rel))
        if text is None:
            continue
        if db.sha(text) != str(row.get("file_hash") or ""):
            run.error(rel, f"{source} is no longer in the store, but this file was edited; left in place")
            run.hold(rel)
            continue
        title = _title_from_path(rel)
        _trash_add(run.owner, rel, title, source, text, "source_gone")
        try:
            os.remove(run.full(rel))
        except OSError as exc:
            run.error(rel, f"could not remove ({exc})")
            continue
        run.scan[rel] = None
        run.drop(rel)
        moved += 1
    if moved:
        run.report["retired"] = run.report.get("retired", 0) + moved
        run.report["notes"].append(f"{moved} note(s) whose source is gone moved to the trash")


# ── import: fold a human's edit of a mirrored file back into its store ──

def _refuse(run: _Run, rel: str, source: str, message: str) -> None:
    run.error(rel, message)
    run.hold(rel, source)


def _process_mem_file(run: _Run, rel: str, fm_data: Dict[str, Any], body: str, item_id: str,
                      base: Optional[Dict[str, Any]]) -> Optional[str]:
    from src import memory_engine as engine

    source = f"mem:{item_id}"
    item = engine.get_item(item_id)
    if not item:
        _refuse(run, rel, source, "this memory no longer exists in the store; the edit was not applied "
                                  "and the file is left as is")
        return None
    if not _owns(item.get("owner"), run.owner):
        _refuse(run, rel, source, "this memory belongs to another owner; nothing was applied")
        return None
    if not render.has_marker(body):
        _refuse(run, rel, source, "the generated-sections marker line is missing, so the memory text "
                                  "cannot be told apart from the generated part; nothing was applied and "
                                  "the file is left as is (put the marker line back)")
        return None
    zone = render.user_zone_of(body).strip()
    if not zone:
        _refuse(run, rel, source, "the memory text is empty; nothing was applied (delete the file to "
                                  "remove the memory)")
        return None
    if len(" ".join(zone.split())) > engine.MAX_TEXT_CHARS:
        _refuse(run, rel, source, f"the memory text is longer than {engine.MAX_TEXT_CHARS} characters; "
                                  "nothing was applied")
        return None
    base_fm = base["fm"] if base else render.memory_note_fields(item)
    base_zone = base["zone"] if base else str(item.get("text") or "")

    changes: Dict[str, Any] = {}
    for key in MEM_EDITABLE:
        if key not in fm_data or _same(key, fm_data.get(key), base_fm.get(key)):
            continue
        try:
            changes[key] = _validate_mem_field(key, fm_data.get(key))
        except ValueError as exc:
            run.error(rel, f"{key}: {exc}; that field was not applied")
    window_from = changes.get("valid_from", item.get("valid_from") or "")
    window_until = changes.get("valid_until", item.get("valid_until") or "")
    parsed_from, parsed_until = db.parse_iso(window_from), db.parse_iso(window_until)
    if parsed_from and parsed_until and parsed_until < parsed_from:
        run.error(rel, "valid_until is before valid_from; the validity window was not changed")
        changes.pop("valid_from", None)
        changes.pop("valid_until", None)

    if not _same_text(zone, base_zone):
        try:
            new_item = engine.correct(item_id, zone, reason="edited in vault", **changes)
        except engine.MemoryEngineError as exc:
            _refuse(run, rel, source, f"the edit could not be applied ({exc}); file left as is")
            return None
        new_source = f"mem:{new_item['id']}"
        row = run.vstate.get(rel)
        if row is not None and row.get("user_path"):
            run.set_source(rel, new_source)  # the human's chosen path now holds the corrected memory
        else:
            run.retire(rel)
        return new_source

    pinned = changes.pop("pinned", None)
    if changes:
        merged = dict(item)
        merged.update(changes)
        merged["updated_at"] = db.now_iso()
        engine.save_item(merged)
    if pinned is not None:
        engine.set_pinned(item_id, pinned)
    return source


def _process_pmem_file(run: _Run, rel: str, fm_data: Dict[str, Any], body: str, entry_id: str,
                       base: Optional[Dict[str, Any]]) -> Optional[str]:
    from src import memory as memmod

    source = f"pmem:{entry_id}"
    mgr = memmod.MemoryManager(_personal_data_dir())
    try:
        entries = mgr.load_all_for_update()
    except memmod.MemoryStoreUnreadable as exc:
        _refuse(run, rel, source, f"personal store unreadable ({exc}); edit not applied")
        return None
    entry = next((e for e in entries if str(e.get("id")) == entry_id), None)
    if entry is None:
        _refuse(run, rel, source, "this personal memory no longer exists; the edit was not applied "
                                  "and the file is left as is")
        return None
    if not _owns(entry.get("owner"), run.owner):
        _refuse(run, rel, source, "this personal memory belongs to another owner; nothing was applied")
        return None
    if not render.has_marker(body):
        _refuse(run, rel, source, "the generated-sections marker line is missing; nothing was applied "
                                  "and the file is left as is (put the marker line back)")
        return None
    zone = render.user_zone_of(body).strip()
    if not zone:
        _refuse(run, rel, source, "the text is empty; nothing was applied (delete the file to remove it)")
        return None
    base_fm = base["fm"] if base else render.personal_note_fields(entry)
    base_zone = base["zone"] if base else str(entry.get("text") or "")
    changed = False
    if not _same_text(zone, base_zone):
        entry["text"] = zone
        changed = True
    if "type" in fm_data and not _same("type", fm_data.get("type"), base_fm.get("type")):
        category = " ".join(str(fm_data.get("type") or "").split())
        if not category or len(category) > 80:
            run.error(rel, "type: must be a short, non-empty word; that field was not applied")
        else:
            entry["category"] = category
            changed = True
    if changed:
        mgr.save(entries)
    return source


def _process_ent_file(run: _Run, rel: str, fm_data: Dict[str, Any], body: str, entity_id: str,
                      base: Optional[Dict[str, Any]]) -> Optional[str]:
    source = f"ent:{entity_id}"
    ent = _entities()
    if ent is None:
        _refuse(run, rel, source, "entities module unavailable, edit not applied")
        return None
    entity = ent.get_entity(entity_id)
    if not entity:
        _refuse(run, rel, source, "this entity no longer exists; the edit was not applied and the file "
                                  "is left as is")
        return None
    if not _owns(entity.get("owner", run.owner), run.owner):
        _refuse(run, rel, source, "this entity belongs to another owner; nothing was applied")
        return None
    if not render.has_marker(body):
        _refuse(run, rel, source, "the generated-sections marker line is missing; nothing was applied "
                                  "and the file is left as is (put the marker line back)")
        return None
    zone = render.user_zone_of(body).strip()
    base_fm = base["fm"] if base else render.entity_note_fields(entity)
    base_zone = base["zone"] if base else str(entity.get("summary") or "")
    name = str(entity.get("name") or "")
    base_stem = base["stem"] if base else db.safe_filename(name)
    updates: Dict[str, Any] = {}

    # The name: an explicit `title:` a human changed wins; otherwise a file
    # name that differs from the one this vault gave it. Never re-derive the
    # name from an unchanged file name (safe_filename is lossy: "Orca#" -> "Orca").
    new_name = ""
    if "title" in fm_data and not _same("title", fm_data.get("title"), base_fm.get("title")):
        new_name = " ".join(str(fm_data.get("title") or "").split())
    else:
        stem = _title_from_path(rel)
        if stem != base_stem and stem != db.safe_filename(name):
            match = _ID_SUFFIX_RE.match(stem)
            if match and str(entity_id).startswith(match.group("id8")):
                stem = match.group("stem")
            if stem != db.safe_filename(name):
                new_name = stem
    if new_name and new_name != name:
        updates["name"] = new_name

    if "type" in fm_data and not _same("type", fm_data.get("type"), base_fm.get("type")):
        choices = tuple(getattr(ent, "TYPES", ()) or ())
        new_type = str(fm_data.get("type") or "").strip().lower()
        if not new_type or (choices and new_type not in choices):
            run.error(rel, f"type: {fm_data.get('type')!r} is not a known entity type; that field was not applied")
        else:
            updates["type"] = new_type
    if "aliases" in fm_data and not _same("aliases", fm_data.get("aliases"), base_fm.get("aliases")):
        updates["aliases"] = _norm("aliases", fm_data.get("aliases"))
    if not _same_text(zone, base_zone):
        updates["summary"] = zone
        # an emptied summary goes back to the automatic one
        updates["summary_locked"] = bool(zone)
    if updates:
        try:
            ent.update_entity(entity_id, **updates)
        except Exception as exc:  # noqa: BLE001 - e.g. an invalid name/type
            _refuse(run, rel, source, f"the edit could not be applied ({exc}); file left as is")
            return None
    return source


def _new_memory_refusal(rel: str, fm_data: Dict[str, Any], text: str) -> str:
    from src import memory_engine as engine
    extra = sorted(k for k in fm_data if k not in NEW_MEMORY_KEYS)
    if extra:
        return f"frontmatter keys a memory cannot keep ({', '.join(extra)})"
    tags = fm_data.get("tags")
    if isinstance(tags, (list, tuple)) and len(tags) > 1:
        return "more than one tag (a memory keeps one category)"
    if len(" ".join(text.split())) > engine.MAX_TEXT_CHARS:
        return f"longer than {engine.MAX_TEXT_CHARS} characters"
    if _STRUCTURED_RE.search(text):
        return "structured markdown (headings, lists, quotes, code or tables)"
    return ""


def _process_new_memory(run: _Run, rel: str, text: str, text_hash: str, fm_data: Dict[str, Any],
                        body: str) -> Optional[Dict[str, Any]]:
    """A new file without `source` under Memories/ becomes one memory — only
    when it fits in one without losing anything; otherwise it stays a free
    note and the refusal is reported (once per content)."""
    from src import memory_engine as engine

    if run.is_refused(rel, text_hash):
        return None
    content = render.user_zone_of(body).strip()
    if not content:
        return None
    reason = _new_memory_refusal(rel, fm_data, content)
    if reason:
        run.error(rel, f"not turned into a memory: {reason}; it stays a free note")
        run.refuse(rel, text_hash)
        return None
    parts = rel.split("/")
    item_type = "fact"
    if len(parts) >= 3:
        for candidate_type, folder_name in render.TYPE_FOLDERS.items():
            if folder_name == parts[1]:
                item_type = candidate_type
                break
    kwargs: Dict[str, Any] = {"owner": run.owner, "type": item_type, "trust_class": "human_explicit"}
    for key in ("type", "level"):
        if fm_data.get(key) not in (None, ""):
            try:
                kwargs[key] = _validate_mem_field(key, fm_data[key])
            except ValueError as exc:
                run.error(rel, f"not turned into a memory: {key}: {exc}")
                run.refuse(rel, text_hash)
                return None
    tags = fm_data.get("tags")
    tag = tags[0] if isinstance(tags, (list, tuple)) and tags else tags
    if tag not in (None, "") and not isinstance(tag, (dict, list)):
        kwargs["category"] = str(tag)
    try:
        item = engine.add_item(content, **kwargs)
    except engine.MemoryEngineError as exc:
        run.error(rel, str(exc))
        run.refuse(rel, text_hash)
        return None
    _trash_add(run.owner, rel, _title_from_path(rel), "", text, "imported")
    run.imported.add(rel)
    run.retire(rel)
    return item


_PROCESSORS = {"mem:": _process_mem_file, "pmem:": _process_pmem_file, "ent:": _process_ent_file}


def _import_path(run: _Run, rel: str, *, missing: Set[str]) -> Tuple[str, str]:
    """Look at one file that is new or changed since this vault last saw it.
    Returns (action, resulting source): action is noop | updated | moved |
    created | error."""
    text, clean = _read_checked(run.full(rel))
    if text is None:
        return "noop", ""
    text_hash = db.sha(text)
    fm_data, body = fm.split(text)
    source = str(fm_data.get("source") or "")
    row = run.sync_rows.get(rel)

    if row is not None:
        expected = str(row.get("source") or "")
        if text_hash == str(row.get("file_hash") or ""):
            run.touch(rel, text_hash)
            return "noop", expected
        if not clean:
            _refuse(run, rel, expected, "not valid UTF-8 text; the edit was not applied and the file is "
                                        "left as is (save it as UTF-8)")
            return "error", expected
        if source != expected:
            _refuse(run, rel, expected, f"the frontmatter `source` should be {expected!r} (it is missing, "
                                        "unreadable or changed); nothing was applied and the file is left as is")
            return "error", expected
        return _apply(run, rel, fm_data, body, source, run.base(rel))

    if rel in run.vstate:  # a generated note (project, home...): export keeps its user zone
        run.touch(rel, text_hash)
        return "noop", str(run.vstate[rel].get("source") or "")

    if source.startswith(MIRRORED_PREFIXES):
        prior = run.paths_of(source)
        gone = [p for p in prior if p in missing]
        if gone:
            old = gone[0]
            missing.discard(old)
            if not clean:
                run.rekey(old, rel, user_path=False)
                _refuse(run, rel, source, "moved here but not valid UTF-8 text; nothing was applied")
                return "error", source
            base = run.base(old)
            run.rekey(old, rel, user_path=not source.startswith("ent:"))
            # applied even when the content is unchanged: an entity's file
            # name IS its name, so a pure rename is an edit
            action, result = _apply(run, rel, fm_data, body, source, base)
            if action == "updated":
                run.report["notes"].append(f"{old} moved to {rel}")
                return "moved", result
            return action, result
        if prior:
            _refuse(run, rel, "", f"a copy of {prior[0]} (same source {source}); nothing was applied — "
                                  "delete one of the two")
            return "error", ""
        # A mirrored note this vault has no record of (brain.db rebuilt, a
        # vault restored from a backup): adopt it, compared with the store.
        if not clean:
            _refuse(run, rel, "", "not valid UTF-8 text; nothing was applied")
            return "error", ""
        home_folder = {"mem:": "Memories/", "pmem:": "Personal/", "ent:": "Entities/"}
        prefix = next(p for p in MIRRORED_PREFIXES if source.startswith(p))
        action, result = _apply(run, rel, fm_data, body, source, None)
        if action == "updated":
            run.adopt(rel, result, text_hash, user_path=not rel.startswith(home_folder[prefix])
                      and prefix != "ent:")
        return action, result

    if not source and rel.startswith("Memories/"):
        if not clean:
            if not run.is_refused(rel, text_hash):
                run.error(rel, "not turned into a memory: not valid UTF-8 text; it stays a free note")
                run.refuse(rel, text_hash)
            return "noop", ""
        item = _process_new_memory(run, rel, text, text_hash, fm_data, body)
        if item:
            return "created", f"mem:{item['id']}"
    return "noop", ""


def _apply(run: _Run, rel: str, fm_data: Dict[str, Any], body: str, source: str,
           base: Optional[Dict[str, Any]]) -> Tuple[str, str]:
    prefix = next(p for p in MIRRORED_PREFIXES if source.startswith(p))
    run.imported.add(rel)
    result = _PROCESSORS[prefix](run, rel, fm_data, body, source[len(prefix):], base)
    if result is None:
        run.imported.discard(rel)
        return "error", source
    run.report["imported"] = run.report.get("imported", 0) + 1
    return "updated", result


def _needs_look(run: _Run, rel: str, notes_idx: Dict[str, Tuple[float, int, str]]) -> bool:
    """False only for a file provably unchanged since this vault last wrote
    or indexed it — the no-op sync reads nothing it already knows."""
    st = run.stat(rel)
    row = run.vstate.get(rel)
    if row is not None:
        return (row.get("mtime_ns"), row.get("size")) != st
    if rel in run.sync_rows:
        return True
    indexed = notes_idx.get(rel)
    if indexed is None or st is None:
        return True
    mtime, size, source = indexed
    if source.startswith(MIRRORED_PREFIXES) or (rel.startswith("Memories/") and not source):
        return True
    return (mtime, size) != (st[0] / 1e9, st[1])


# ── deletions: a mirrored file disappeared ──────────────────────────────

def _apply_deletions(run: _Run, missing_rows: List[Dict[str, Any]]) -> Set[str]:
    """Suppress/remove/hide whatever a missing mirrored file stood for, or —
    if too many disappeared at once — trip the guard and touch nothing.
    Returns the sources the guard blocked, so the export phase that follows
    knows not to silently put those files right back this run."""
    from src.settings import get_setting

    report = run.report
    ratio = float(get_setting("brain_vault_delete_guard_ratio", 0.3) or 0.3)
    mirrored_total = len([r for r in run.sync_rows.values()
                          if str(r.get("source") or "").startswith(MIRRORED_PREFIXES)])
    threshold = max(5, ratio * mirrored_total)
    if len(missing_rows) > threshold:
        report["guard_tripped"] = True
        report["notes"].append(
            f"delete guard: {len(missing_rows)} mirrored files missing "
            f"(> {threshold:.0f} of {mirrored_total}); no deletions applied this run"
        )
        return {row["source"] for row in missing_rows}
    for row in missing_rows:
        source, path = row["source"], row["path"]
        try:
            if source.startswith("mem:"):
                _delete_mem(run.owner, source[4:], path, report)
            elif source.startswith("pmem:"):
                _delete_pmem(run.owner, source[5:], path, report)
            elif source.startswith("ent:"):
                _delete_ent(run.owner, source[4:], path, report)
        except Exception as exc:  # noqa: BLE001 - one bad row must not sink the run
            logger.exception("brain vault: deletion failed for %s", path)
            report["errors"].append(f"{path}: {exc}")
            continue
        run.drop(path)
    return set()


def _delete_mem(owner: str, item_id: str, path: str, report: Dict[str, Any]) -> None:
    from src import memory_engine as engine

    item = engine.get_item(item_id)
    if not item or not _owns(item.get("owner"), owner):
        return
    content = render.render_memory_note(item, entities=_entity_links_for(f"mem:{item_id}"))
    _trash_add(owner, path, render.memory_title_text(item), f"mem:{item_id}", content, "suppressed")
    engine.set_suppressed(item_id, True)
    report["suppressed"] += 1


def _delete_pmem(owner: str, entry_id: str, path: str, report: Dict[str, Any]) -> None:
    from src import memory as memmod

    mgr = memmod.MemoryManager(_personal_data_dir())
    try:
        entries = mgr.load_all_for_update()
    except memmod.MemoryStoreUnreadable as exc:
        report["errors"].append(f"{path}: personal store unreadable, deletion skipped ({exc})")
        return
    target = next((e for e in entries if str(e.get("id")) == entry_id), None)
    if target is None or not _owns(target.get("owner"), owner):
        return
    content = render.render_personal_note(target)
    mgr.save([e for e in entries if str(e.get("id")) != entry_id])
    _trash_add(owner, path, str(target.get("text") or "")[:80], f"pmem:{entry_id}", content, "removed")
    report["suppressed"] += 1


def _delete_ent(owner: str, entity_id: str, path: str, report: Dict[str, Any]) -> None:
    ent = _entities()
    if ent is None:
        report["errors"].append(f"{path}: entities module unavailable, deletion skipped")
        return
    entity = ent.get_entity(entity_id)
    if not entity or not _owns(entity.get("owner", owner), owner):
        return
    content = render.render_entity_note(entity, _entity_profile(entity))
    ent.set_hidden(entity_id, True)
    _trash_add(owner, path, str(entity.get("name") or ""), f"ent:{entity_id}", content, "hidden")
    report["suppressed"] += 1


# ── the public entry points ──────────────────────────────────────────────

def _new_report() -> Dict[str, Any]:
    return {
        "at": db.now_iso(), "exported": 0, "imported": 0, "created": 0,
        "suppressed": 0, "retired": 0, "conflicts": [], "guard_tripped": False, "errors": [],
        "duration_ms": 0, "notes": [],
    }


def _notes_index(owner: str) -> Dict[str, Tuple[float, int, str]]:
    with db.db() as conn:
        rows = conn.execute("SELECT path, mtime, size, source FROM notes WHERE owner=?", (owner,)).fetchall()
    return {r["path"]: (float(r["mtime"] or 0), int(r["size"] or 0), str(r["source"] or "")) for r in rows}


def sync(owner: str = "", *, budget_s: float = 30.0) -> Dict[str, Any]:
    """Import changed files, apply deletions, export the stores to files,
    retire notes whose source is gone, then reindex. See the module
    docstring for the rules."""
    owner = str(owner or "")
    report = _new_report()
    os.makedirs(vault_root(owner), exist_ok=True)
    run = _Run(owner, report, budget_s=budget_s)
    try:
        # -- 1. scan (stat only) and import what changed
        for rel, full in _iter_files(run.root):
            run.scan[rel] = _stat(full)
        missing = {rel for rel in run.sync_rows if run.scan.get(rel) is None}
        notes_idx = _notes_index(owner)
        for rel in sorted(p for p, st in run.scan.items() if st is not None):
            if run.over_budget():
                report["notes"].append("import phase stopped early: time budget exceeded")
                run.import_complete = False
                break
            if not _needs_look(run, rel, notes_idx):
                continue
            try:
                action, _ = _import_path(run, rel, missing=missing)
                if action == "created":
                    report["created"] += 1
                    report["imported"] += 1
            except Exception as exc:  # noqa: BLE001 - one bad file must not sink the run
                logger.exception("brain vault: import failed for %s", rel)
                run.error(rel, str(exc))
                run.hold(rel)

        # -- 2. deletions (guarded; never on a partial scan — an unexamined
        # file could be the moved copy of a "missing" one)
        skip_sources: Set[str] = set()
        missing_rows = [run.sync_rows[p] for p in sorted(missing) if p in run.sync_rows]
        if missing_rows and run.import_complete:
            skip_sources = _apply_deletions(run, missing_rows)
        elif missing_rows:
            skip_sources = {r["source"] for r in missing_rows}

        # -- 3. export: the stores -> files
        plan = _build_plan(run)
        _export_all(run, plan, skip_sources)

        # -- 4. notes whose source left the store leave the vault
        _retire_gone_sources(run, plan)
    finally:
        try:
            run.flush()
        except Exception as exc:  # noqa: BLE001
            logger.exception("brain vault: saving sync state failed")
            report["errors"].append(f"state: {exc}")

    # -- 5. reindex (search/graph/backlinks over the vault as it now stands)
    try:
        from . import notes as notes_mod
        notes_mod.reindex(owner)
    except Exception as exc:  # noqa: BLE001
        logger.exception("brain vault: reindex failed")
        report["errors"].append(f"reindex: {exc}")

    report["duration_ms"] = int((time.time() - run.start) * 1000)
    _record_run(owner, report)
    return report


def _export_sources(run: _Run, sources: List[str]) -> Dict[str, str]:
    plan = _build_plan(run)
    out: Dict[str, str] = {}
    for source in sources:
        target = plan.targets.get(source)
        if target is None:
            continue
        rel = _export_target(run, plan, target)
        if rel:
            out[source] = rel
    return out


def _export_single(owner: str, source: str) -> Optional[str]:
    """Re-render and (if needed) write just one source right now — used
    right after a single-file import or an entity edit so the UI sees the
    result immediately, without waiting for the next full sync."""
    owner = str(owner or "")
    run = _Run(owner, _new_report())
    try:
        return _export_sources(run, [source]).get(source)
    finally:
        run.flush()


def import_file(owner: str, rel_path: str) -> Dict[str, Any]:
    """Import ONE file right now (used right after `notes.write_note`), then
    re-export just that source and reindex — a human should not have to wait
    for the next periodic sync to see their own edit take effect."""
    owner = str(owner or "")
    rel_path = validate_path(rel_path)
    run = _Run(owner, _new_report())
    if run.stat(rel_path) is None:
        return {"action": "missing", "path": rel_path}
    result: Dict[str, Any] = {"action": "noop", "source": "", "errors": [], "path": rel_path}
    try:
        missing = {p for p in run.sync_rows if p != rel_path and run.stat(p) is None}
        action, source = _import_path(run, rel_path, missing=missing)
        result["action"] = action
        result["source"] = source
        if source and action in ("updated", "moved", "created"):
            exported = _export_sources(run, [source])
            if exported.get(source):
                result["path"] = exported[source]
    except Exception as exc:  # noqa: BLE001
        logger.exception("brain vault: single import failed for %s", rel_path)
        result["action"] = "error"
        run.error(rel_path, str(exc))
    finally:
        run.flush()
    result["errors"] = list(run.report["errors"])
    try:
        from . import notes as notes_mod
        notes_mod.reindex(owner)
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(f"reindex: {exc}")
    return result


# ── helpers kept for callers of the previous module API ─────────────────

def _memory_target(item: Dict[str, Any]):
    """(source, rel_path, render_fn) of one memory note, rendered from the
    store (render_fn's argument is ignored: a memory's text is its store
    text). Its path is the canonical one, before collision suffixes."""
    source = f"mem:{item['id']}"
    folder = render.TYPE_FOLDERS.get(item.get("type"), "Facts")
    rel_path = f"Memories/{folder}/{render.memory_note_title(item)}.md"
    entity_links = _entity_links_for(source)

    def render_fn(_uz: str = "", item=item, entity_links=entity_links) -> str:
        return render.render_memory_note(item, entities=entity_links)

    return source, rel_path, render_fn
