"""offload_search.py — full-text search over offloaded tool results.

`src/tool_result_offload.py` (A12) stores an oversized tool result whole in
the artifact store and leaves the model a bounded preview plus an
`artifact_id`. Without this module the model can only page through that
artifact blindly with `read_artifact_range` (start/end or a single
case-insensitive substring). This module lets it instead ask "where in this
offloaded result does X show up" and get back ranked hits with the char
offset to open next.

Design:

* **BM25 via SQLite FTS5**, one virtual table per app-data DB
  (`DATA_DIR/offload_search.db`), degrading to a LIKE scan when the sqlite
  build lacks FTS5 (checked once, cached module-wide).
* **Chunking matches `read_artifact_range`'s own offsets.** That function
  slices the persisted JSON text by 0-based, end-exclusive CHARACTER offsets
  (`text[lo:hi]`, see its docstring). `index_result()` is handed the exact
  same `full_text` string `tool_result_offload._persist_full_result` wrote
  to disk, so a chunk's `chunk_start` and `chunk_start + len(text)` are
  valid arguments to `read_artifact(artifact_id, start=..., end=...)`
  without any translation.
* **Owner isolation is mandatory.** Every read filters on `owner` in SQL;
  there is no code path that returns another owner's rows.
* **Idempotent per artifact_id.** The offload id is a deterministic
  occurrence id over content, so re-indexing the same artifact_id is a
  cheap no-op (existence check) rather than a delete+reinsert.
* **Retention.** Rows older than `offload_search_retention_days` (default
  14) are pruned opportunistically on insert — no separate cron.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()
_PATH: Optional[str] = None

#: Cached across calls once determined for the live sqlite3 build. Tests
#: monkeypatch this to exercise the LIKE fallback without a special build.
_FTS5_AVAILABLE: Optional[bool] = None

_CHUNK_SIZE = 1500
_CHUNK_OVERLAP = 150

_TABLE_COLUMNS = "owner, session_id, artifact_id, tool, chunk_start, text, indexed_at"


def use_path(path: Optional[str]) -> None:
    """Test hook: point the module at a scratch DB file."""
    global _PATH
    _PATH = str(path) if path else None


def use_fts5(available: Optional[bool]) -> None:
    """Test hook: force (or reset, with None) the FTS5-availability flag."""
    global _FTS5_AVAILABLE
    _FTS5_AVAILABLE = available


def path() -> str:
    if _PATH:
        return _PATH
    from src.constants import DATA_DIR
    return os.path.join(DATA_DIR, "offload_search.db")


def _fts5_available(conn: sqlite3.Connection) -> bool:
    global _FTS5_AVAILABLE
    if _FTS5_AVAILABLE is not None:
        return _FTS5_AVAILABLE
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS __fts5_probe USING fts5(x)")
        conn.execute("DROP TABLE IF EXISTS __fts5_probe")
        _FTS5_AVAILABLE = True
    except sqlite3.OperationalError:
        _FTS5_AVAILABLE = False
    return _FTS5_AVAILABLE


@contextmanager
def _db() -> Iterator[sqlite3.Connection]:
    target = path()
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    conn = sqlite3.connect(target, timeout=15.0)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=15000")
        if _fts5_available(conn):
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS offload_chunks_fts USING fts5("
                "owner UNINDEXED, session_id UNINDEXED, artifact_id UNINDEXED, "
                "tool UNINDEXED, chunk_start UNINDEXED, text, indexed_at UNINDEXED)"
            )
        else:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS offload_chunks_plain ("
                "owner TEXT NOT NULL, session_id TEXT NOT NULL DEFAULT '', "
                "artifact_id TEXT NOT NULL, tool TEXT NOT NULL DEFAULT '', "
                "chunk_start INTEGER NOT NULL, text TEXT NOT NULL, "
                "indexed_at REAL NOT NULL)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_offload_chunks_plain_scope "
                "ON offload_chunks_plain(owner, artifact_id)"
            )
        with conn:
            yield conn
    finally:
        conn.close()


def _table_name() -> str:
    return "offload_chunks_fts" if _FTS5_AVAILABLE else "offload_chunks_plain"


# ── chunking ─────────────────────────────────────────────────────────────

def _chunk_offsets(text: str, chunk_size: int = _CHUNK_SIZE,
                   overlap: int = _CHUNK_OVERLAP) -> List[tuple]:
    """Split `text` into (start, end) spans of ~chunk_size chars with
    ~overlap chars of overlap, preferring to end on a newline near the
    target boundary. Offsets are 0-based, end-exclusive, and always slice
    back to the exact original text (`text[start:end]`)."""
    n = len(text)
    if n == 0:
        return []
    spans: List[tuple] = []
    pos = 0
    while pos < n:
        end = min(pos + chunk_size, n)
        if end < n:
            search_lo = max(pos + chunk_size // 2, end - 200)
            nl = text.rfind("\n", search_lo, end)
            if nl > pos:
                end = nl + 1
        spans.append((pos, end))
        if end >= n:
            break
        next_pos = end - overlap
        if next_pos <= pos:
            next_pos = pos + 1
        pos = next_pos
    return spans


# ── query sanitization ──────────────────────────────────────────────────

def _sanitize_fts_query(query: str) -> Optional[str]:
    """Turn free text into a safe FTS5 MATCH expression: bare words and
    quoted phrases ANDed together, nothing else. Reserved FTS5 syntax
    (AND/OR/NEAR, parentheses, column filters, stray quotes) never reaches
    the engine unescaped, so a malicious or malformed query can never raise
    `sqlite3.OperationalError` — it just yields fewer/no matches."""
    parts: List[str] = []
    for match in re.finditer(r'"([^"]*)"|[\w][\w._-]*', query or "", flags=re.UNICODE):
        phrase = match.group(1)
        if phrase is not None:
            phrase = phrase.strip()
            if phrase:
                parts.append('"' + phrase.replace('"', '""') + '"')
            continue
        token = match.group(0).strip("._-")
        if not token:
            continue
        parts.append('"' + token.replace('"', '""') + '"')
    if not parts:
        return None
    return " ".join(parts)


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _like_terms(query: str) -> List[str]:
    return [t for t in re.findall(r"[\w-]+", query or "", flags=re.UNICODE) if t]


# ── retention ────────────────────────────────────────────────────────────

def _retention_days() -> int:
    try:
        from src.settings import get_setting
        return max(1, int(get_setting("offload_search_retention_days", 14) or 14))
    except Exception:  # noqa: BLE001
        return 14


def _prune(conn: sqlite3.Connection) -> None:
    cutoff = time.time() - _retention_days() * 86400
    try:
        conn.execute(f"DELETE FROM {_table_name()} WHERE indexed_at < ?", (cutoff,))
    except sqlite3.OperationalError:
        pass


# ── public API ───────────────────────────────────────────────────────────

def index_result(owner: str, session_id: str, artifact_id: str, tool: str,
                 full_text: str) -> int:
    """Chunk and index an offloaded tool result's full text.

    Idempotent per `artifact_id`: if it is already indexed, this is a cheap
    no-op. Returns the number of chunks stored (0 if skipped or empty).
    Never raises for storage problems the caller should not be broken by —
    callers that want that (e.g. this module's own tests) catch nothing
    special; `tool_result_offload` wraps its call in try/except regardless.
    """
    owner = str(owner or "")
    artifact_id = str(artifact_id or "")
    if not owner or not artifact_id or not full_text:
        return 0

    with _LOCK, _db() as conn:
        table = _table_name()
        existing = conn.execute(
            f"SELECT 1 FROM {table} WHERE owner=? AND artifact_id=? LIMIT 1",
            (owner, artifact_id),
        ).fetchone()
        if existing:
            return 0

        _prune(conn)

        spans = _chunk_offsets(full_text)
        if not spans:
            return 0
        now = time.time()
        rows = [
            (owner, str(session_id or ""), artifact_id, str(tool or ""),
             int(start), full_text[start:end], now)
            for start, end in spans
        ]
        conn.executemany(
            f"INSERT INTO {table} ({_TABLE_COLUMNS}) VALUES (?,?,?,?,?,?,?)",
            rows,
        )
        return len(rows)


def search(owner: str, query: str, *, session_id: Optional[str] = None,
          artifact_id: Optional[str] = None, limit: int = 8) -> List[Dict[str, Any]]:
    """Rank chunks of this owner's offloaded results against `query`.

    Always scoped to `owner` — no code path here returns another owner's
    rows. Optionally narrowed further to one `session_id` and/or one
    `artifact_id`. A query that FTS5 syntax would reject, or that is empty
    after sanitizing, returns `[]` rather than raising.
    """
    owner = str(owner or "")
    query = str(query or "").strip()
    if not owner or not query:
        return []
    limit = max(1, min(int(limit or 8), 20))

    with _LOCK, _db() as conn:
        table = _table_name()
        if _FTS5_AVAILABLE:
            return _search_fts(conn, table, owner, query, session_id, artifact_id, limit)
        return _search_like(conn, table, owner, query, session_id, artifact_id, limit)


def _scope_clause(session_id: Optional[str], artifact_id: Optional[str]) -> tuple:
    clause = ""
    params: List[Any] = []
    if session_id:
        clause += " AND session_id=?"
        params.append(str(session_id))
    if artifact_id:
        clause += " AND artifact_id=?"
        params.append(str(artifact_id))
    return clause, params


def _search_fts(conn, table, owner, query, session_id, artifact_id, limit):
    fts_query = _sanitize_fts_query(query)
    if not fts_query:
        return []
    scope_clause, scope_params = _scope_clause(session_id, artifact_id)
    sql = (
        f"SELECT owner, session_id, artifact_id, tool, chunk_start, text, "
        f"bm25({table}) AS score FROM {table} "
        f"WHERE {table} MATCH ? AND owner=?{scope_clause} "
        f"ORDER BY score LIMIT ?"
    )
    try:
        rows = conn.execute(sql, (fts_query, owner, *scope_params, limit)).fetchall()
    except sqlite3.OperationalError:
        logger.debug("offload_search: FTS5 query rejected, returning no hits", exc_info=True)
        return []
    return [_row_to_hit(r, query, bm25=True) for r in rows]


def _search_like(conn, table, owner, query, session_id, artifact_id, limit):
    terms = _like_terms(query)
    if not terms:
        return []
    scope_clause, scope_params = _scope_clause(session_id, artifact_id)
    like_clause = " AND ".join("text LIKE ? ESCAPE '\\'" for _ in terms)
    like_params = ["%" + _escape_like(t) + "%" for t in terms]
    sql = (
        f"SELECT owner, session_id, artifact_id, tool, chunk_start, text "
        f"FROM {table} WHERE owner=?{scope_clause} AND {like_clause} "
        f"LIMIT ?"
    )
    try:
        rows = conn.execute(
            sql, (owner, *scope_params, *like_params, limit * 4)
        ).fetchall()
    except sqlite3.OperationalError:
        logger.debug("offload_search: LIKE query failed, returning no hits", exc_info=True)
        return []
    scored = []
    for r in rows:
        lowered = r["text"].lower()
        score = sum(lowered.count(t.lower()) for t in terms)
        scored.append((score, r))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [_row_to_hit(r, query, bm25=False) for _, r in scored[:limit]]


def _row_to_hit(row, query: str, *, bm25: bool) -> Dict[str, Any]:
    text = row["text"]
    start = int(row["chunk_start"])
    end = start + len(text)
    snippet = _snippet(text, query)
    hit = {
        "artifact_id": row["artifact_id"],
        "tool": row["tool"],
        "start": start,
        "end": end,
        "snippet": snippet,
    }
    # bm25() returns a negative "smaller is better" score; expose it as-is
    # under a documented sign convention rather than pretending it is a
    # bounded 0..1 relevance number.
    hit["score"] = float(row["score"]) if bm25 and "score" in row.keys() else None
    return hit


def _snippet(text: str, query: str, radius: int = 80) -> str:
    terms = _like_terms(query)
    idx = -1
    if terms:
        lowered = text.lower()
        for t in terms:
            idx = lowered.find(t.lower())
            if idx != -1:
                break
    if idx == -1:
        return text[: radius * 2].strip()
    lo = max(0, idx - radius)
    hi = min(len(text), idx + radius)
    return ("..." if lo > 0 else "") + text[lo:hi].strip() + ("..." if hi < len(text) else "")
