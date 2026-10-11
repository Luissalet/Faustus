"""SQLite storage for observations and turn summaries (``OBSERVATIONS_DB``).

One file, WAL, opened per call (a connection is cheap and the worker thread,
the request threads and the agent loop all use this module). Tables are
created idempotently on first use of a path. The full-text index is an FTS5
table keyed by the observation id; when this SQLite build has no FTS5 the
index is simply not created and :func:`search` falls back to ``LIKE`` over the
same columns — the answer is the same shape, only the ranking is weaker.

Reads never raise past this module for a missing or unreadable file: the
public functions return empty results and log at debug level, because a
broken recall store must not take a chat or an MCP call down with it.

Owner scoping: every read takes ``owner``. A non-empty owner sees only its own
rows; an empty owner is the single-user / unauthenticated case and sees all.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .schema import Observation, TurnSummary

logger = logging.getLogger(__name__)

SEARCH_MAX = 20
DETAILS_MAX = 5
TIMELINE_SIDE_MAX = 10
ANCHORS_MAX = 5

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS observations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner TEXT NOT NULL DEFAULT '',
        session_id TEXT NOT NULL DEFAULT '',
        turn_id TEXT NOT NULL DEFAULT '',
        created REAL NOT NULL,
        type TEXT NOT NULL,
        title TEXT NOT NULL,
        subtitle TEXT NOT NULL DEFAULT '',
        facts TEXT NOT NULL DEFAULT '[]',
        narrative TEXT NOT NULL DEFAULT '',
        concepts TEXT NOT NULL DEFAULT '[]',
        files_read TEXT NOT NULL DEFAULT '[]',
        files_modified TEXT NOT NULL DEFAULT '[]',
        tool TEXT NOT NULL DEFAULT '',
        project TEXT NOT NULL DEFAULT '',
        workspace TEXT NOT NULL DEFAULT '',
        fingerprint TEXT NOT NULL DEFAULT ''
    )""",
    "CREATE INDEX IF NOT EXISTS idx_obs_project ON observations(owner, project, created)",
    "CREATE INDEX IF NOT EXISTS idx_obs_session ON observations(session_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_obs_fp ON observations(session_id, fingerprint)",
    """CREATE TABLE IF NOT EXISTS observation_files (
        obs_id INTEGER NOT NULL,
        path TEXT NOT NULL,
        base TEXT NOT NULL,
        role TEXT NOT NULL,
        PRIMARY KEY (obs_id, path, role)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_obsfiles_base ON observation_files(base)",
    """CREATE TABLE IF NOT EXISTS summaries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner TEXT NOT NULL DEFAULT '',
        session_id TEXT NOT NULL DEFAULT '',
        turn_id TEXT NOT NULL DEFAULT '',
        created REAL NOT NULL,
        project TEXT NOT NULL DEFAULT '',
        workspace TEXT NOT NULL DEFAULT '',
        request TEXT NOT NULL DEFAULT '',
        investigated TEXT NOT NULL DEFAULT '',
        learned TEXT NOT NULL DEFAULT '',
        completed TEXT NOT NULL DEFAULT '',
        next_steps TEXT NOT NULL DEFAULT '',
        from_obs_id INTEGER NOT NULL DEFAULT 0,
        to_obs_id INTEGER NOT NULL DEFAULT 0,
        obs_count INTEGER NOT NULL DEFAULT 0
    )""",
    "CREATE INDEX IF NOT EXISTS idx_sum_project ON summaries(owner, project, created)",
    "CREATE INDEX IF NOT EXISTS idx_sum_session ON summaries(session_id, to_obs_id)",
)
_FTS_DDL = ("CREATE VIRTUAL TABLE IF NOT EXISTS observations_fts USING fts5("
            "title, subtitle, facts, narrative, tokenize='unicode61 remove_diacritics 2')")

_READY: Dict[str, bool] = {}          # db path -> has FTS5
_READY_LOCK = threading.Lock()


# -- connection -------------------------------------------------------------

def db_path() -> str:
    """Resolved at call time so a test (or a relocated data dir) can point
    ``src.constants.OBSERVATIONS_DB`` somewhere else."""
    from src import constants
    return str(constants.OBSERVATIONS_DB)


def _ensure_schema(conn: sqlite3.Connection, path: str) -> bool:
    with _READY_LOCK:
        if path in _READY:
            return _READY[path]
        for statement in _SCHEMA:
            conn.execute(statement)
        fts = True
        try:
            conn.execute(_FTS_DDL)
        except sqlite3.OperationalError:
            fts = False
        conn.commit()
        _READY[path] = fts
        return fts


def connect() -> sqlite3.Connection:
    path = db_path()
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    for pragma in ("PRAGMA journal_mode=WAL", "PRAGMA synchronous=NORMAL"):
        with contextlib.suppress(sqlite3.Error):
            conn.execute(pragma)
    try:
        _ensure_schema(conn, path)
    except sqlite3.Error:
        with contextlib.suppress(sqlite3.Error):
            conn.close()
        with _READY_LOCK:
            _READY.pop(path, None)
        raise
    return conn


def fts_available() -> bool:
    try:
        conn = connect()
    except sqlite3.Error:
        return False
    try:
        return bool(_READY.get(db_path()))
    finally:
        conn.close()


def reset_for_tests() -> None:
    with _READY_LOCK:
        _READY.clear()


# -- helpers ----------------------------------------------------------------

def iso(ts: float) -> str:
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError, TypeError):
        return ""


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _loads(text: Any, default: Any) -> Any:
    try:
        value = json.loads(text) if isinstance(text, str) else text
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def norm_path(path: str, workspace: str = "") -> str:
    """A path as stored and compared: forward slashes, relative to the
    workspace when it lies inside it, no leading ``./``, lower-cased on Windows."""
    text = str(path or "").strip().replace("\\", "/")
    root = str(workspace or "").strip().replace("\\", "/").rstrip("/")
    if root and (text == root or text.startswith(root + "/")):
        text = text[len(root):].lstrip("/")
    while text.startswith("./"):
        text = text[2:]
    return text.lower() if os.name == "nt" else text


def _same_file(a: str, b: str) -> bool:
    return a == b or a.endswith("/" + b) or b.endswith("/" + a)


def _owner_clause(owner: str, column: str = "owner") -> Tuple[str, List[Any]]:
    return (f"{column} = ?", [owner]) if owner else ("1=1", [])


def _index_row(row: sqlite3.Row) -> Dict[str, Any]:
    return {"id": row["id"], "created": iso(row["created"]), "type": row["type"], "title": row["title"]}


def _observation_record(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "kind": "observation", "ref": str(row["id"]), "id": row["id"],
        "created": iso(row["created"]), "ts": row["created"],
        "type": row["type"], "title": row["title"], "subtitle": row["subtitle"],
        "facts": _loads(row["facts"], []), "narrative": row["narrative"],
        "concepts": _loads(row["concepts"], []),
        "files_read": _loads(row["files_read"], []), "files_modified": _loads(row["files_modified"], []),
        "tool": row["tool"], "session_id": row["session_id"], "turn_id": row["turn_id"],
        "project": row["project"], "workspace": row["workspace"], "owner": row["owner"],
    }


def _summary_record(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "kind": "summary", "ref": f"S{row['id']}", "id": row["id"],
        "created": iso(row["created"]), "ts": row["created"], "type": "summary",
        "title": row["request"],
        "request": row["request"], "investigated": row["investigated"], "learned": row["learned"],
        "completed": row["completed"], "next_steps": row["next_steps"],
        "observations": [row["from_obs_id"], row["to_obs_id"]] if row["obs_count"] else [],
        "obs_count": row["obs_count"],
        "session_id": row["session_id"], "turn_id": row["turn_id"],
        "project": row["project"], "workspace": row["workspace"],
    }


def parse_ref(ref: Any) -> Optional[Tuple[str, int]]:
    """``12`` / ``"12"`` / ``"#12"`` -> ("observation", 12); ``"S3"`` -> ("summary", 3)."""
    if isinstance(ref, bool):
        return None
    if isinstance(ref, int):
        return ("observation", ref) if ref > 0 else None
    text = str(ref or "").strip().lstrip("#")
    if re.fullmatch(r"[sS]\d+", text):
        return ("summary", int(text[1:]))
    if re.fullmatch(r"\d+", text) and int(text) > 0:
        return ("observation", int(text))
    return None


# -- writes -----------------------------------------------------------------

def add_observation(obs: Observation, *, owner: str = "", session_id: str = "", turn_id: str = "",
                    tool: str = "", project: str = "", workspace: str = "",
                    created: Optional[float] = None) -> Optional[int]:
    """Store one observation; returns its id, or None for a duplicate (same
    chat, type and title already stored). Raises sqlite3.Error on a storage
    failure — the worker logs and drops."""
    fingerprint = hashlib.sha1(
        f"{session_id}\x00{obs.type}\x00{obs.title.lower()}".encode("utf-8", errors="replace")
    ).hexdigest()[:16]
    stamp = float(created if created is not None else time.time())
    conn = connect()
    try:
        if session_id and conn.execute(
                "SELECT 1 FROM observations WHERE session_id = ? AND fingerprint = ? LIMIT 1",
                (session_id, fingerprint)).fetchone():
            return None
        cur = conn.execute(
            "INSERT INTO observations (owner, session_id, turn_id, created, type, title, subtitle, facts, "
            "narrative, concepts, files_read, files_modified, tool, project, workspace, fingerprint) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (owner, session_id, turn_id, stamp, obs.type, obs.title, obs.subtitle, _dumps(obs.facts),
             obs.narrative, _dumps(obs.concepts), _dumps(obs.files_read), _dumps(obs.files_modified),
             tool, project, workspace, fingerprint))
        obs_id = int(cur.lastrowid)
        for role, paths in (("read", obs.files_read), ("modified", obs.files_modified)):
            for path in paths:
                normalized = norm_path(path, workspace)
                if normalized:
                    conn.execute(
                        "INSERT OR IGNORE INTO observation_files (obs_id, path, base, role) VALUES (?,?,?,?)",
                        (obs_id, normalized, normalized.rsplit("/", 1)[-1], role))
        if _READY.get(db_path()):
            try:
                conn.execute(
                    "INSERT INTO observations_fts (rowid, title, subtitle, facts, narrative) VALUES (?,?,?,?,?)",
                    (obs_id, obs.title, obs.subtitle, "\n".join(obs.facts), obs.narrative))
            except sqlite3.Error:
                # The note is kept; only its index entry is missing (the LIKE path still finds it).
                logger.debug("observations: index insert failed for %s", obs_id, exc_info=True)
        conn.commit()
        return obs_id
    finally:
        conn.close()


def add_summary(summary: TurnSummary, *, owner: str = "", session_id: str = "", turn_id: str = "",
                project: str = "", workspace: str = "", from_obs_id: int = 0, to_obs_id: int = 0,
                obs_count: int = 0, created: Optional[float] = None) -> int:
    stamp = float(created if created is not None else time.time())
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO summaries (owner, session_id, turn_id, created, project, workspace, request, "
            "investigated, learned, completed, next_steps, from_obs_id, to_obs_id, obs_count) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (owner, session_id, turn_id, stamp, project, workspace, summary.request, summary.investigated,
             summary.learned, summary.completed, summary.next_steps, from_obs_id, to_obs_id, obs_count))
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def delete_observation(obs_id: int, owner: str = "") -> bool:
    clause, params = _owner_clause(owner)
    conn = connect()
    try:
        row = conn.execute(f"SELECT id FROM observations WHERE id = ? AND {clause}", [obs_id, *params]).fetchone()
        if not row:
            return False
        _delete_ids(conn, [obs_id])
        conn.commit()
        return True
    finally:
        conn.close()


def _delete_ids(conn: sqlite3.Connection, ids: Sequence[int]) -> None:
    for chunk_start in range(0, len(ids), 400):
        chunk = list(ids[chunk_start:chunk_start + 400])
        marks = ",".join("?" * len(chunk))
        conn.execute(f"DELETE FROM observation_files WHERE obs_id IN ({marks})", chunk)
        if _READY.get(db_path()):
            conn.execute(f"DELETE FROM observations_fts WHERE rowid IN ({marks})", chunk)
        conn.execute(f"DELETE FROM observations WHERE id IN ({marks})", chunk)


def prune(retention_days: int, *, now: Optional[float] = None) -> Dict[str, int]:
    """Delete observations and summaries older than `retention_days` (0 = keep all)."""
    if not retention_days or retention_days <= 0:
        return {"observations": 0, "summaries": 0}
    cutoff = float(now if now is not None else time.time()) - retention_days * 86400.0
    conn = connect()
    try:
        ids = [r["id"] for r in conn.execute("SELECT id FROM observations WHERE created < ?", (cutoff,))]
        if ids:
            _delete_ids(conn, ids)
        gone = conn.execute("DELETE FROM summaries WHERE created < ?", (cutoff,)).rowcount
        conn.commit()
        return {"observations": len(ids), "summaries": int(gone or 0)}
    finally:
        conn.close()


# -- reads: the three steps -------------------------------------------------

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def fts_query(query: str, *, mode: str = "and") -> str:
    """A syntactically safe FTS5 expression from free text: every word quoted
    (so operators in the text are just words), words of four letters or more
    as prefixes (a cheap stand-in for stemming), at most 12 words."""
    words = [w.lower() for w in _TOKEN_RE.findall(query or "")][:12]
    parts = [f'"{w}"*' if len(w) >= 4 else f'"{w}"' for w in words]
    return (" OR " if mode == "or" else " AND ").join(parts)


def _filters(owner: str, project: str, kind: str, since: Optional[float], session_id: str = "") -> Tuple[str, List[Any]]:
    clauses, params = [], []
    if owner:
        clauses.append("o.owner = ?")
        params.append(owner)
    if project:
        clauses.append("o.project = ?")
        params.append(project)
    if kind:
        clauses.append("o.type = ?")
        params.append(kind)
    if since:
        clauses.append("o.created >= ?")
        params.append(float(since))
    if session_id:
        clauses.append("o.session_id = ?")
        params.append(session_id)
    return (" AND ".join(clauses) if clauses else "1=1"), params


def search(query: str, *, limit: int = SEARCH_MAX, owner: str = "", project: str = "", kind: str = "",
           since: Optional[float] = None, session_id: str = "") -> List[Dict[str, Any]]:
    """Step 1. The index only: ``{id, created, type, title}`` per hit, best
    first. An empty query is the most recent. Never raises."""
    limit = max(1, min(int(limit or SEARCH_MAX), SEARCH_MAX))
    if not str(query or "").strip():
        return recent(limit=limit, owner=owner, project=project, kind=kind, since=since, session_id=session_id)
    where, params = _filters(owner, project, kind, since, session_id)
    try:
        conn = connect()
    except sqlite3.Error:
        logger.debug("observations: store unavailable for search", exc_info=True)
        return []
    try:
        if _READY.get(db_path()):
            for mode in ("and", "or"):
                expression = fts_query(query, mode=mode)
                if not expression:
                    break
                try:
                    rows = conn.execute(
                        "SELECT o.id, o.created, o.type, o.title FROM observations_fts f "
                        "JOIN observations o ON o.id = f.rowid "
                        f"WHERE observations_fts MATCH ? AND {where} "
                        "ORDER BY bm25(observations_fts), o.created DESC LIMIT ?",
                        [expression, *params, limit]).fetchall()
                except sqlite3.OperationalError:
                    break
                if rows:
                    return [_index_row(r) for r in rows]
            else:
                return []
        return _like_search(conn, query, where, params, limit)
    except sqlite3.Error:
        logger.debug("observations: search failed", exc_info=True)
        return []
    finally:
        conn.close()


def _like_pattern(word: str) -> str:
    escaped = word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _like_search(conn: sqlite3.Connection, query: str, where: str, params: List[Any], limit: int) -> List[Dict[str, Any]]:
    """The fallback without FTS5: every word must occur (then, failing that,
    any word), newest first."""
    words = [w.lower() for w in _TOKEN_RE.findall(query)][:8]
    if not words:
        return []
    column = "lower(o.title || ' ' || o.subtitle || ' ' || o.facts || ' ' || o.narrative) LIKE ? ESCAPE '\\'"
    patterns = [_like_pattern(w) for w in words]
    for joiner in (" AND ", " OR "):
        rows = conn.execute(
            f"SELECT o.id, o.created, o.type, o.title FROM observations o WHERE {where} AND ("
            + joiner.join(column for _ in words) + ") ORDER BY o.created DESC LIMIT ?",
            [*params, *patterns, limit]).fetchall()
        if rows or len(words) == 1:
            return [_index_row(r) for r in rows]
    return []


def recent(*, limit: int = SEARCH_MAX, owner: str = "", project: str = "", kind: str = "",
           since: Optional[float] = None, session_id: str = "") -> List[Dict[str, Any]]:
    limit = max(1, min(int(limit or SEARCH_MAX), 200))
    where, params = _filters(owner, project, kind, since, session_id)
    try:
        conn = connect()
    except sqlite3.Error:
        return []
    try:
        rows = conn.execute(
            f"SELECT o.id, o.created, o.type, o.title FROM observations o WHERE {where} "
            "ORDER BY o.created DESC, o.id DESC LIMIT ?", [*params, limit]).fetchall()
        return [_index_row(r) for r in rows]
    except sqlite3.Error:
        logger.debug("observations: recent failed", exc_info=True)
        return []
    finally:
        conn.close()


def _timeline_entry(row: sqlite3.Row, kind: str, anchor: bool) -> Dict[str, Any]:
    if kind == "summary":
        return {"ref": f"S{row['id']}", "kind": "summary", "id": row["id"], "created": iso(row["created"]),
                "type": "summary", "title": row["request"], "anchor": anchor, "_ts": row["created"]}
    return {"ref": str(row["id"]), "kind": "observation", "id": row["id"], "created": iso(row["created"]),
            "type": row["type"], "title": row["title"], "anchor": anchor, "_ts": row["created"]}


def timeline(anchors: Iterable[Any], *, before: int = 2, after: int = 2, owner: str = "") -> List[Dict[str, Any]]:
    """Step 2. Chronological neighbours around up to five anchors (observation
    ids, or ``S<n>`` for a summary): `before` earlier and `after` later
    observations of the same project (or, for an anchor with no project, the
    same chat), plus the summaries that fall inside the window. Entries carry
    ``ref``, ``kind``, ``created``, ``type``, ``title`` and ``anchor``."""
    before = max(0, min(int(before), TIMELINE_SIDE_MAX))
    after = max(0, min(int(after), TIMELINE_SIDE_MAX))
    parsed = [p for p in (parse_ref(a) for a in list(anchors)[:ANCHORS_MAX]) if p]
    if not parsed:
        return []
    try:
        conn = connect()
    except sqlite3.Error:
        return []
    owner_sql, owner_params = _owner_clause(owner)
    entries: Dict[Tuple[str, int], Dict[str, Any]] = {}
    try:
        for kind, ident in parsed:
            table = "summaries" if kind == "summary" else "observations"
            anchor = conn.execute(f"SELECT * FROM {table} WHERE id = ? AND {owner_sql}", [ident, *owner_params]).fetchone()
            if not anchor:
                continue
            entries[(kind, ident)] = _timeline_entry(anchor, kind, True)
            scope_sql, scope_params = (("project = ?", [anchor["project"]]) if anchor["project"]
                                       else ("session_id = ?", [anchor["session_id"]]))
            stamp = anchor["created"]
            skip_id = ident if kind == "observation" else -1
            earlier = conn.execute(
                f"SELECT * FROM observations WHERE {owner_sql} AND {scope_sql} AND id != ? AND "
                "(created < ? OR (created = ? AND id < ?)) ORDER BY created DESC, id DESC LIMIT ?",
                [*owner_params, *scope_params, skip_id, stamp, stamp, ident if kind == "observation" else 2 ** 62, before]
            ).fetchall() if before else []
            later = conn.execute(
                f"SELECT * FROM observations WHERE {owner_sql} AND {scope_sql} AND id != ? AND "
                "(created > ? OR (created = ? AND id > ?)) ORDER BY created ASC, id ASC LIMIT ?",
                [*owner_params, *scope_params, skip_id, stamp, stamp, ident if kind == "observation" else 0, after]
            ).fetchall() if after else []
            for row in [*earlier, *later]:
                entries.setdefault(("observation", row["id"]), _timeline_entry(row, "observation", False))
            window = [e["_ts"] for e in entries.values()]
            low, high = min(window), max(window)
            summaries = conn.execute(
                f"SELECT * FROM summaries WHERE {owner_sql} AND {scope_sql} AND created BETWEEN ? AND ?",
                [*owner_params, *scope_params, low, high]).fetchall()
            for row in summaries:
                entries.setdefault(("summary", row["id"]), _timeline_entry(row, "summary", False))
    except sqlite3.Error:
        logger.debug("observations: timeline failed", exc_info=True)
        return []
    finally:
        conn.close()
    ordered = sorted(entries.values(), key=lambda e: (e["_ts"], 0 if e["kind"] == "observation" else 1, e["id"]))
    for entry in ordered:
        entry.pop("_ts", None)
    return ordered


def details(refs: Iterable[Any], *, owner: str = "") -> List[Dict[str, Any]]:
    """Step 3. The full records of up to five ids (``12`` or ``S3``), in the
    order asked; ids that do not exist (or belong to someone else) are left out."""
    parsed = [p for p in (parse_ref(r) for r in list(refs)[:DETAILS_MAX]) if p]
    if not parsed:
        return []
    owner_sql, owner_params = _owner_clause(owner)
    try:
        conn = connect()
    except sqlite3.Error:
        return []
    out: List[Dict[str, Any]] = []
    try:
        for kind, ident in parsed:
            if kind == "summary":
                row = conn.execute(f"SELECT * FROM summaries WHERE id = ? AND {owner_sql}", [ident, *owner_params]).fetchone()
                if row:
                    out.append(_summary_record(row))
            else:
                row = conn.execute(f"SELECT * FROM observations WHERE id = ? AND {owner_sql}", [ident, *owner_params]).fetchone()
                if row:
                    out.append(_observation_record(row))
    except sqlite3.Error:
        logger.debug("observations: details failed", exc_info=True)
    finally:
        conn.close()
    return out


# -- file lookup, summaries, stats ------------------------------------------

def for_file(path: str, *, limit: int = 5, owner: str = "", workspace: str = "") -> List[Dict[str, Any]]:
    """Newest observations whose files_read or files_modified include `path`
    (matched on the workspace-relative path, so the same file under another
    checkout root still matches). Index shape plus ``role`` (read / modified)."""
    limit = max(1, min(int(limit or 5), 50))
    wanted = norm_path(path, workspace)
    if not wanted:
        return []
    base = wanted.rsplit("/", 1)[-1]
    owner_sql, owner_params = _owner_clause(owner, "o.owner")
    try:
        conn = connect()
    except sqlite3.Error:
        return []
    try:
        rows = conn.execute(
            "SELECT o.id, o.created, o.type, o.title, f.path AS fpath, f.role AS role "
            "FROM observation_files f JOIN observations o ON o.id = f.obs_id "
            f"WHERE f.base = ? AND {owner_sql} ORDER BY o.created DESC, o.id DESC LIMIT 400",
            [base, *owner_params]).fetchall()
    except sqlite3.Error:
        logger.debug("observations: file lookup failed", exc_info=True)
        return []
    finally:
        conn.close()
    out: List[Dict[str, Any]] = []
    seen = set()
    for row in rows:
        if row["id"] in seen or not _same_file(row["fpath"], wanted):
            continue
        seen.add(row["id"])
        entry = _index_row(row)
        entry["role"] = row["role"]
        out.append(entry)
        if len(out) >= limit:
            break
    return out


def turn_observations(session_id: str, turn_id: str = "", *, limit: int = 60) -> List[Dict[str, Any]]:
    """The observations of one turn, oldest first, as full records — empty when
    that turn already has a summary. Without `turn_id`: the chat's observations
    newer than its last summary."""
    if not session_id:
        return []
    try:
        conn = connect()
    except sqlite3.Error:
        return []
    try:
        if turn_id:
            if conn.execute("SELECT 1 FROM summaries WHERE session_id = ? AND turn_id = ? LIMIT 1",
                            (session_id, turn_id)).fetchone():
                return []
            rows = conn.execute(
                "SELECT * FROM observations WHERE session_id = ? AND turn_id = ? ORDER BY id ASC LIMIT ?",
                (session_id, turn_id, max(1, int(limit)))).fetchall()
        else:
            last = conn.execute("SELECT COALESCE(MAX(to_obs_id), 0) AS m FROM summaries WHERE session_id = ?",
                                (session_id,)).fetchone()["m"]
            rows = conn.execute(
                "SELECT * FROM observations WHERE session_id = ? AND id > ? ORDER BY id ASC LIMIT ?",
                (session_id, last, max(1, int(limit)))).fetchall()
        return [_observation_record(r) for r in rows]
    except sqlite3.Error:
        logger.debug("observations: turn_observations failed", exc_info=True)
        return []
    finally:
        conn.close()


def recent_records(*, project: str = "", owner: str = "", limit: int = 60) -> List[Dict[str, Any]]:
    """Newest full observation records of a project (for the session recap)."""
    where, params = _filters(owner, project, "", None)
    try:
        conn = connect()
    except sqlite3.Error:
        return []
    try:
        rows = conn.execute(f"SELECT o.* FROM observations o WHERE {where} ORDER BY o.created DESC, o.id DESC LIMIT ?",
                            [*params, max(1, int(limit))]).fetchall()
        return [_observation_record(r) for r in rows]
    except sqlite3.Error:
        logger.debug("observations: recent_records failed", exc_info=True)
        return []
    finally:
        conn.close()


def last_summary(*, project: str = "", owner: str = "") -> Optional[Dict[str, Any]]:
    clauses, params = ["1=1"], []
    if owner:
        clauses.append("owner = ?")
        params.append(owner)
    if project:
        clauses.append("project = ?")
        params.append(project)
    try:
        conn = connect()
    except sqlite3.Error:
        return None
    try:
        row = conn.execute(f"SELECT * FROM summaries WHERE {' AND '.join(clauses)} ORDER BY created DESC, id DESC LIMIT 1",
                           params).fetchone()
        return _summary_record(row) if row else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def stats(owner: str = "") -> Dict[str, Any]:
    owner_sql, owner_params = _owner_clause(owner)
    try:
        conn = connect()
    except sqlite3.Error:
        return {"observations": 0, "summaries": 0, "fts": False}
    try:
        obs = conn.execute(f"SELECT COUNT(*) AS n, MAX(created) AS last FROM observations WHERE {owner_sql}",
                           owner_params).fetchone()
        summ = conn.execute(f"SELECT COUNT(*) AS n FROM summaries WHERE {owner_sql}", owner_params).fetchone()
        return {"observations": obs["n"], "summaries": summ["n"], "last": iso(obs["last"]) if obs["last"] else "",
                "fts": bool(_READY.get(db_path()))}
    except sqlite3.Error:
        return {"observations": 0, "summaries": 0, "fts": False}
    finally:
        conn.close()
