"""Persistent coordination spaces; execution remains in the existing engines."""

from __future__ import annotations

import re
import sqlite3
import uuid
from typing import Any, Callable

DEFAULT_SPACE_ID = "ecosystem"
_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,95}$")

# Additive migration: original IDs and records remain in the same tables.
SPACES_MIGRATION = """
CREATE TABLE coordination_spaces (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
    project_id TEXT NOT NULL DEFAULT '', workspace TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
    created REAL NOT NULL, updated REAL NOT NULL);
INSERT INTO coordination_spaces(id, title, created_by, created, updated)
    VALUES('ecosystem', 'Faustus y Hoards', 'system', 0, 0);
ALTER TABLE tasks ADD COLUMN space_id TEXT NOT NULL DEFAULT 'ecosystem';
ALTER TABLE threads ADD COLUMN space_id TEXT NOT NULL DEFAULT 'ecosystem';
CREATE INDEX tasks_space ON tasks(space_id, status, priority);
CREATE INDEX threads_space ON threads(space_id, status, updated);
CREATE TABLE coordination_members (
    space_id TEXT NOT NULL REFERENCES coordination_spaces(id),
    agent TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY(space_id, agent));
INSERT INTO coordination_members(space_id, agent, created)
    SELECT 'ecosystem', id, COALESCE(created, 0) FROM agents;
CREATE TABLE coordination_reads (
    space_id TEXT NOT NULL REFERENCES coordination_spaces(id), agent TEXT NOT NULL,
    last_msg_id INTEGER NOT NULL DEFAULT 0, updated REAL NOT NULL,
    PRIMARY KEY(space_id, agent));
"""


class SpaceError(ValueError):
    def __init__(self, message: str, status: int = 400, **extra: Any):
        super().__init__(message)
        self.status = status
        self.extra = extra


def space_id(value: Any) -> str:
    if value in (None, ""):
        return DEFAULT_SPACE_ID
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise SpaceError("space_id must be a stable identifier (letters, digits, dot, dash or underscore)")
    return value


def _text(value: Any, field: str, *, required: bool = False, limit: int = 20000) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise SpaceError(f"{field} must be {'non-empty ' if required else ''}text up to {limit} characters")
    return value.strip()


class SpaceStore:
    """Use the canonical coordination database, never a per-client replica."""

    def __init__(self, db: Any, *, clock: Callable[[], float]):
        self.db = db
        self.clock = clock

    def get(self, value: Any = None) -> dict[str, Any]:
        sid = space_id(value)
        row = self.db.one("SELECT * FROM coordination_spaces WHERE id=?", (sid,))
        if row is None:
            raise SpaceError(f"no space {sid}", 404)
        result = dict(row)
        result["members"] = [r["agent"] for r in self.db.query(
            "SELECT agent FROM coordination_members WHERE space_id=? ORDER BY agent", (sid,))]
        return result

    def list(self) -> list[dict[str, Any]]:
        return [self.get(r["id"]) for r in self.db.query(
            "SELECT id FROM coordination_spaces ORDER BY updated DESC, id")]

    def create(self, data: dict[str, Any], *, agent: str) -> dict[str, Any]:
        sid = space_id(data.get("id") or f"space_{uuid.uuid4().hex}")
        now = self.clock()
        fields = {"id": sid, "title": _text(data.get("title"), "title", required=True, limit=200),
                  "description": _text(data.get("description"), "description"),
                  "project_id": _text(data.get("project_id"), "project_id", limit=256),
                  "workspace": _text(data.get("workspace"), "workspace", limit=2048),
                  "created_by": agent, "created": now, "updated": now}
        try:
            with self.db.tx():
                self.db.insert("coordination_spaces", fields)
                self.join(sid, agent)
        except sqlite3.IntegrityError as exc:
            raise SpaceError(f"space {sid} already exists", 409) from exc
        return self.get(sid)

    def update(self, data: dict[str, Any]) -> dict[str, Any]:
        sid = space_id(data.get("space_id"))
        expected = data.get("expected_revision")
        if type(expected) is not int or expected < 1:
            raise SpaceError("expected_revision must be a positive integer")
        changes = {key: _text(data[key], key, required=key == "title", limit=limit)
                   for key, limit in (("title", 200), ("description", 20000),
                                      ("project_id", 256), ("workspace", 2048)) if key in data}
        with self.db.tx():
            current = self.get(sid)
            if current["revision"] != expected:
                raise SpaceError("space revision changed", 409, error_code="stale_space",
                                 current_revision=current["revision"])
            if changes:
                changes.update(revision=expected + 1, updated=self.clock())
                self.db.execute("UPDATE coordination_spaces SET " + ", ".join(f"{key}=?" for key in changes)
                                + " WHERE id=?", (*changes.values(), sid))
        return self.get(sid)

    def join(self, sid: str, agent: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO coordination_members(space_id, agent, created) VALUES(?,?,?)",
                        (sid, agent, self.clock()))

    def read_upto(self, sid: str, agent: str) -> int:
        # Preserve the existing default-space cursor, also used by older clients.
        table = "reads" if sid == DEFAULT_SPACE_ID else "coordination_reads"
        where, params = ("agent=?", (agent,)) if sid == DEFAULT_SPACE_ID else (
            "space_id=? AND agent=?", (sid, agent))
        row = self.db.one(f"SELECT last_msg_id FROM {table} WHERE {where}", params)
        return int(row["last_msg_id"] or 0) if row else 0

    def mark_read(self, sid: str, agent: str, upto: int) -> None:
        if sid == DEFAULT_SPACE_ID:
            self.db.execute("INSERT INTO reads(agent, last_msg_id, updated) VALUES(?,?,?) ON CONFLICT(agent) DO "
                            "UPDATE SET last_msg_id=MAX(reads.last_msg_id, excluded.last_msg_id), updated=excluded.updated",
                            (agent, upto, self.clock()))
        else:
            self.db.execute("INSERT INTO coordination_reads(space_id, agent, last_msg_id, updated) VALUES(?,?,?,?) "
                            "ON CONFLICT(space_id, agent) DO UPDATE SET last_msg_id="
                            "MAX(coordination_reads.last_msg_id, excluded.last_msg_id), updated=excluded.updated",
                            (sid, agent, upto, self.clock()))
