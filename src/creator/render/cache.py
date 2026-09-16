"""cache.py — WP14: the render cache, ``DATA_DIR/creator/render_cache.db``.

Same pattern as ``src/budget_account.py`` / ``src/harness_evolution/store.py``
(CONTRATO.md rule 2): a private sqlite file, opened and closed per call, WAL
journal mode, a real ``BEGIN IMMEDIATE`` for the one write this module makes,
append-only rows (a cache entry is never mutated after it is written, only
looked up or, explicitly, evicted).

**Cache key.** ``compute_key()`` hashes exactly the things that can change
what a render WOULD produce, nothing else:

* the exact resolved inputs, as their CONTENT hash (``blob_sha256`` of each
  occurrence, in the graph's own deterministic input order) — not the
  occurrence id, so two different occurrences of byte-identical content
  still hit the same cache entry, and an occurrence id reused for DIFFERENT
  bytes (should never happen, but defence in depth) never collides;
* the document's own ``revision`` — any edit invalidates the key even if an
  edit happened to leave the referenced occurrences unchanged;
* the render params (profile id + every profile field, via
  ``Profile.cache_fingerprint()`` — not just its id, so redefining a named
  profile's bitrate is itself a cache-busting change) and the subtitle
  occurrence's content hash, when one is burned;
* the resolved ``ffmpeg -version`` build string — a different local ffmpeg
  build is not guaranteed to produce byte-identical output for the same
  filtergraph, so it is NOT treated as "the same render".

**Discipline the ficha calls out explicitly**: this cache is for the
FFmpeg composition path only. A hit reuses the OUTPUT occurrence of a prior
render with the exact same key — it never reuses a generative result
(ComfyUI/TTS/music/... adapters do not call into this module) under a
"deterministic" key that would misrepresent them as reproducible.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

VERSION = 1


def default_path() -> Path:
    from src.constants import DATA_DIR
    return Path(DATA_DIR) / "creator" / "render_cache.db"


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS render_cache (
            cache_key TEXT PRIMARY KEY,
            owner TEXT NOT NULL,
            project_id TEXT NOT NULL,
            doc_id TEXT NOT NULL,
            output_occurrence_id TEXT NOT NULL,
            engine_build TEXT NOT NULL,
            profile_id TEXT NOT NULL,
            created_at REAL NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS ix_render_cache_doc ON render_cache(owner, doc_id)")


@contextmanager
def _db(path: Optional[Path] = None, *, write: bool = False):
    path = path or default_path()
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        if write:
            conn.execute("BEGIN IMMEDIATE")
        _create_schema(conn)
        yield conn
        if write:
            conn.commit()
    except Exception:
        if write:
            conn.rollback()
        raise
    finally:
        conn.close()


def compute_key(*, input_hashes: Sequence[str], doc_revision: int,
                 profile_fingerprint: Sequence[Any], subtitle_hash: str = "",
                 engine_build: str, extra_params: Optional[Mapping[str, Any]] = None) -> str:
    """A deterministic sha256 over exactly the fields named in the module
    docstring — a canonical (sorted-keys, no whitespace) JSON document, so
    the same logical inputs ALWAYS hash to the same key regardless of
    argument construction order."""
    payload = {
        "v": VERSION,
        "input_hashes": list(input_hashes),
        "doc_revision": int(doc_revision),
        "profile_fingerprint": list(profile_fingerprint),
        "subtitle_hash": subtitle_hash or "",
        "engine_build": engine_build or "",
        "extra_params": dict(extra_params or {}),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def lookup(cache_key: str, *, owner: str, path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """The cached row for ``cache_key`` scoped to ``owner`` (CONTRATO.md rule
    3 — a cache is not a way to read another owner's render), or ``None`` on
    a miss. Does NOT check that the output occurrence still exists — the
    caller (``service.py``) does that against the real artifact store, since
    only it knows how to react to "the cached output was since deleted"."""
    with _db(path, write=False) as conn:
        row = conn.execute(
            "SELECT * FROM render_cache WHERE cache_key = ? AND owner = ?",
            (cache_key, owner or ""),
        ).fetchone()
        return dict(row) if row is not None else None


def store(cache_key: str, *, owner: str, project_id: str, doc_id: str,
          output_occurrence_id: str, engine_build: str, profile_id: str,
          path: Optional[Path] = None) -> Dict[str, Any]:
    """Insert a cache entry, idempotently: a second ``store()`` for the SAME
    key (the deterministic-by-construction common case — the same inputs
    really did produce the same output again) is a no-op that returns the
    FIRST row ever written, never overwritten (CAS via ``INSERT OR IGNORE``
    inside a real transaction, the same discipline as ``budget_account``'s
    own compare-and-swap writes)."""
    with _db(path, write=True) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO render_cache
                (cache_key, owner, project_id, doc_id, output_occurrence_id,
                 engine_build, profile_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (cache_key, owner or "", project_id or "", doc_id or "",
             output_occurrence_id, engine_build or "", profile_id or "", time.time()),
        )
        row = conn.execute(
            "SELECT * FROM render_cache WHERE cache_key = ? AND owner = ?",
            (cache_key, owner or ""),
        ).fetchone()
        return dict(row) if row is not None else {}


__all__ = ["default_path", "compute_key", "lookup", "store", "VERSION"]
