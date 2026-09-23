"""
brain/db.py — one SQLite file for everything the brain derives.

Same policy as ``src.context_engine.store`` (and for the same reasons):
short-lived WAL connections under a module lock, schema registered by the
module that owns the tables, a corrupt file moved aside instead of deleted.
The file is ``DATA_DIR/brain/brain.db``; tests point it elsewhere with
``use_dir(tmp_path)``.
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
import unicodedata
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()
_SCHEMAS: "Dict[str, Tuple[str, ...]]" = {}
_DIR_OVERRIDE: Optional[str] = None


class BrainStoreError(RuntimeError):
    """brain.db could not be opened. Callers on a turn path degrade on this."""


def _default_data_dir() -> str:
    try:
        from src.constants import DATA_DIR
        return DATA_DIR
    except Exception:  # pragma: no cover - import-time fallback
        return os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data")


def brain_dir() -> str:
    """``DATA_DIR/brain`` — home of brain.db and, by default, the vault."""
    return _DIR_OVERRIDE or os.path.join(_default_data_dir(), "brain")


def use_dir(path: Optional[str]) -> None:
    """Point the whole brain (db + default vault) somewhere else. Tests only."""
    global _DIR_OVERRIDE
    with _LOCK:
        _DIR_OVERRIDE = path


def db_path() -> str:
    return os.path.join(brain_dir(), "brain.db")


def register_schema(name: str, statements: Sequence[str]) -> None:
    """Declare the tables a module owns. Idempotent DDL only; runs on every open."""
    with _LOCK:
        _SCHEMAS[str(name)] = tuple(str(s) for s in statements if str(s).strip())


def _apply_schema(conn: sqlite3.Connection) -> None:
    with _LOCK:
        blocks = list(_SCHEMAS.items())
    for name, statements in blocks:
        for statement in statements:
            try:
                conn.execute(statement)
            except sqlite3.Error as exc:
                logger.error("brain schema %s failed: %s", name, exc)


def _quarantine(path: str, reason: Any) -> None:
    for suffix in ("", "-wal", "-shm"):
        victim = path + suffix
        if os.path.exists(victim):
            try:
                os.replace(victim, victim + ".corrupt")
            except OSError:
                with contextlib.suppress(OSError):
                    os.unlink(victim)
    logger.warning("brain.db was unusable (%s); moved aside and recreated", reason)


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10.0)
    try:
        conn.row_factory = sqlite3.Row
        with contextlib.suppress(sqlite3.Error):
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        _apply_schema(conn)
        conn.commit()
        return conn
    except Exception:
        with contextlib.suppress(Exception):
            conn.close()
        raise


def _open() -> sqlite3.Connection:
    path = db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        return _connect(path)
    except sqlite3.DatabaseError as exc:
        _quarantine(path, exc)
        try:
            return _connect(path)
        except sqlite3.Error as exc2:  # pragma: no cover - dead disk
            raise BrainStoreError(f"brain store unusable: {exc2}") from exc2


@contextlib.contextmanager
def db():
    """A short-lived connection under the module lock, committed on success."""
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


# ── shared helpers ──────────────────────────────────────────────────────────

def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def dumps(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return "null"


def loads(raw: Any, default: Any = None) -> Any:
    if raw in (None, ""):
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def sha(text: Any) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def fold(text: Any) -> str:
    """Case- and accent-insensitive key: 'Móstoles ' -> 'mostoles'."""
    raw = unicodedata.normalize("NFKD", str(text or ""))
    raw = "".join(ch for ch in raw if not unicodedata.combining(ch))
    return " ".join(raw.casefold().split())


_SLUG_BAD = re.compile(r'[\\/:*?"<>|#^\[\]\x00-\x1f]+')


def safe_filename(title: Any, *, limit: int = 80) -> str:
    """A title usable as a file name on Windows and in a [[wikilink]].

    Keeps accents and spaces (Obsidian-style names), drops the characters
    Windows or wikilinks cannot hold, and never returns a reserved name."""
    text = _SLUG_BAD.sub(" ", str(title or "")).strip().strip(".")
    text = " ".join(text.split())[:limit].rstrip(" .")
    if not text:
        text = "untitled"
    if text.upper() in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(r"(?i)(COM|LPT)\d", text):
        text = f"_{text}"
    return text


def owner_key(owner: Any) -> str:
    """Folder name for an owner's vault ('' -> '_shared')."""
    return safe_filename(owner or "_shared", limit=60)
