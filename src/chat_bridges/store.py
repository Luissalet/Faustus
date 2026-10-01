"""src/chat_bridges/store.py — what a chat bridge remembers between restarts.

A small sqlite file under the data folder (`DATA_DIR/chat_bridges/telegram.sqlite3`):

* `chats`   — which Faustus conversation a chat is mapped to;
* `refused` — chats that were told once they are not authorised, so a stranger
              who keeps writing is answered once and then ignored;
* `meta`    — the last `getUpdates` offset, so a restart does not hand the same
              messages to the agent twice.

Nothing secret is stored here (no token, no message text).
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    chat_id    TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    title      TEXT NOT NULL DEFAULT '',
    created    REAL NOT NULL,
    updated    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS refused (
    chat_id TEXT PRIMARY KEY,
    title   TEXT NOT NULL DEFAULT '',
    ts      REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def default_path(name: str = "telegram.sqlite3") -> str:
    from src.constants import DATA_DIR
    folder = os.path.join(DATA_DIR, "chat_bridges")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, name)


class BridgeStore:
    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or default_path()
        self._lock = threading.Lock()
        with self._connect() as db:
            db.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def _run(self, sql: str, args: tuple = ()) -> List[sqlite3.Row]:
        with self._lock:
            db = self._connect()
            try:
                rows = db.execute(sql, args).fetchall()
                db.commit()
                return rows
            finally:
                db.close()

    # ── chat → session ──────────────────────────────────────────────────
    def get_session(self, chat_id: str) -> Optional[Dict[str, Any]]:
        rows = self._run("SELECT * FROM chats WHERE chat_id = ?", (str(chat_id),))
        return dict(rows[0]) if rows else None

    def set_session(self, chat_id: str, session_id: str, title: str = "") -> None:
        now = time.time()
        self._run(
            "INSERT INTO chats(chat_id, session_id, title, created, updated) VALUES(?,?,?,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET session_id=excluded.session_id, "
            "title=excluded.title, updated=excluded.updated",
            (str(chat_id), session_id, title[:200], now, now),
        )

    def touch(self, chat_id: str) -> None:
        self._run("UPDATE chats SET updated = ? WHERE chat_id = ?", (time.time(), str(chat_id)))

    def list_sessions(self) -> List[Dict[str, Any]]:
        return [dict(r) for r in self._run("SELECT * FROM chats ORDER BY updated DESC")]

    # ── refused chats ───────────────────────────────────────────────────
    def was_refused(self, chat_id: str) -> bool:
        return bool(self._run("SELECT 1 FROM refused WHERE chat_id = ?", (str(chat_id),)))

    def mark_refused(self, chat_id: str, title: str = "") -> None:
        self._run("INSERT OR REPLACE INTO refused(chat_id, title, ts) VALUES(?,?,?)",
                  (str(chat_id), title[:200], time.time()))

    def list_refused(self) -> List[Dict[str, Any]]:
        return [dict(r) for r in self._run("SELECT * FROM refused ORDER BY ts DESC")]

    def forget_refused(self, chat_id: str) -> None:
        self._run("DELETE FROM refused WHERE chat_id = ?", (str(chat_id),))

    # ── meta ────────────────────────────────────────────────────────────
    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        rows = self._run("SELECT value FROM meta WHERE key = ?", (key,))
        return rows[0]["value"] if rows else default

    def set_meta(self, key: str, value: str) -> None:
        self._run("INSERT OR REPLACE INTO meta(key, value) VALUES(?,?)", (key, str(value)))
