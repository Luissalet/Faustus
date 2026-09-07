"""Durable, owner-scoped lexical index for project context links.

The index contains only derived chunks.  ``projects.json`` remains the truth
about membership and source revision, and every reader must re-authorise the
source through its resolver before returning a stored chunk.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence

from src.constants import PROJECT_CONTEXT_INDEX_DB
from .models import ExtractedCorpus, SourceMatch

_PATH: Optional[str] = None
_LOCK = threading.RLock()
_WORD = re.compile(r"[\w-]+", re.UNICODE)


def use_path(path: Optional[str]) -> None:
    global _PATH
    _PATH = str(path) if path else None


def path() -> str:
    return _PATH or PROJECT_CONTEXT_INDEX_DB


def _db() -> sqlite3.Connection:
    target = path()
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    conn = sqlite3.connect(target, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS project_context_indexes ("
        "owner TEXT NOT NULL, project_id TEXT NOT NULL, link_id TEXT NOT NULL, "
        "revision TEXT NOT NULL, chunk_count INTEGER NOT NULL DEFAULT 0, "
        "indexed_at REAL NOT NULL, PRIMARY KEY(owner, project_id, link_id))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS project_context_chunks ("
        "owner TEXT NOT NULL, project_id TEXT NOT NULL, link_id TEXT NOT NULL, "
        "chunk_index INTEGER NOT NULL, revision TEXT NOT NULL, text TEXT NOT NULL, "
        "title TEXT NOT NULL DEFAULT '', location TEXT NOT NULL DEFAULT '{}', "
        "media_type TEXT NOT NULL DEFAULT '', indexed_at REAL NOT NULL, "
        "PRIMARY KEY(owner, project_id, link_id, chunk_index))"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_project_context_scope "
        "ON project_context_chunks(owner, project_id, link_id, revision)"
    )
    return conn


def replace(*, owner: str, project_id: str, link_id: str,
            corpus: ExtractedCorpus) -> int:
    """Atomically replace one link's chunks and return the stored count."""
    holder, project, link = str(owner), str(project_id), str(link_id)
    if not holder or not project or not link:
        raise ValueError("owner, project_id and link_id are required")
    now = time.time()
    rows = []
    for chunk in corpus.chunks:
        text = str(chunk.text or "")
        if not text.strip():
            continue
        rows.append((holder, project, link, int(chunk.index),
                     str(corpus.revision or ""), text,
                     str(chunk.title or "")[:1000],
                     json.dumps(dict(chunk.location or {}), ensure_ascii=False,
                                sort_keys=True, default=str),
                     str(chunk.media_type or ""), now))
    with _LOCK, _db() as conn:
        # Publish the marker and all chunks in one transaction. The marker also
        # represents an empty but successfully indexed source.
        conn.execute(
            "INSERT OR REPLACE INTO project_context_indexes "
            "(owner,project_id,link_id,revision,chunk_count,indexed_at) "
            "VALUES (?,?,?,?,?,?)",
            (holder, project, link, str(corpus.revision or ""), len(rows), now),
        )
        conn.execute(
            "DELETE FROM project_context_chunks WHERE owner=? AND project_id=? AND link_id=?",
            (holder, project, link),
        )
        if rows:
            conn.executemany(
                "INSERT INTO project_context_chunks "
                "(owner,project_id,link_id,chunk_index,revision,text,title,location,media_type,indexed_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
    return len(rows)


def indexed_revision(*, owner: str, project_id: str, link_id: str) -> str:
    with _LOCK, _db() as conn:
        row = conn.execute(
            "SELECT revision FROM project_context_indexes "
            "WHERE owner=? AND project_id=? AND link_id=?",
            (str(owner), str(project_id), str(link_id)),
        ).fetchone()
    return str(row["revision"] or "") if row else ""


def search(*, owner: str, project_id: str, link_id: str, query: str,
           limit: int = 20) -> List[SourceMatch]:
    """Rank exact phrase/token occurrences inside one authorised link."""
    needle = str(query or "").strip().casefold()
    if not needle:
        return []
    tokens = list(dict.fromkeys(_WORD.findall(needle)))[:32]
    with _LOCK, _db() as conn:
        rows = conn.execute(
            "SELECT * FROM project_context_chunks "
            "WHERE owner=? AND project_id=? AND link_id=? ORDER BY chunk_index",
            (str(owner), str(project_id), str(link_id)),
        ).fetchall()
    ranked = []
    for row in rows:
        text = str(row["text"] or "")
        folded = text.casefold()
        phrase_hits = folded.count(needle)
        token_hits = sum(folded.count(token) for token in tokens)
        if phrase_hits <= 0 and token_hits <= 0:
            continue
        score = float(phrase_hits * 10 + token_hits) / max(1.0, len(text) / 400.0)
        try:
            location = json.loads(str(row["location"] or "{}"))
        except (TypeError, ValueError):
            location = {}
        ranked.append((score, int(row["chunk_index"]), SourceMatch(
            location=location if isinstance(location, Mapping) else {},
            snippet=text[:400], score=score, revision=str(row["revision"] or ""),
        )))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    cap = max(1, min(int(limit or 20), 200))
    return [item[2] for item in ranked[:cap]]


def delete(*, owner: str, project_id: str, link_id: str) -> int:
    with _LOCK, _db() as conn:
        cur = conn.execute(
            "DELETE FROM project_context_chunks WHERE owner=? AND project_id=? AND link_id=?",
            (str(owner), str(project_id), str(link_id)),
        )
        marker = conn.execute(
            "DELETE FROM project_context_indexes WHERE owner=? AND project_id=? AND link_id=?",
            (str(owner), str(project_id), str(link_id)),
        )
        return max(0, int(cur.rowcount or 0)) + max(0, int(marker.rowcount or 0))


__all__ = ["use_path", "path", "replace", "indexed_revision", "search", "delete"]
