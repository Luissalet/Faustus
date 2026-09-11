"""Small owner-scoped SQLite document/event store for durable subsystems.

Teach, Immune and Branching own different databases and domain contracts, but
need the same boring guarantees: WAL, atomic revision checks, owner isolation,
and append-only events.  Keeping those guarantees here avoids three subtly
different implementations of tenancy and optimistic concurrency.

Reads intentionally fail closed (``None``/``[]``).  Writes raise
``StoreError`` so routes can return a stable refusal without losing a turn.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Mapping, Optional

from src.contracts.base import now_iso

logger = logging.getLogger(__name__)


class StoreError(RuntimeError):
    pass


class RevisionConflict(StoreError):
    pass


class DurableFeatureStore:
    def __init__(self, path: str) -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._init()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """One transaction on a connection that is CLOSED on the way out.

        `sqlite3.Connection` as a context manager commits or rolls back but
        never closes, so every `with self._connect()` left a handle (and the
        WAL side files) open until garbage collection — harmless on Linux,
        `WinError 32` on Windows the moment a temporary directory holding
        `futures.db` is removed.
        """
        connection = sqlite3.connect(self.path, timeout=10.0)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=10000")
            with connection:
                yield connection
        finally:
            connection.close()

    def _init(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    kind TEXT NOT NULL,
                    id TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    project_id TEXT NOT NULL DEFAULT '',
                    session_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT '',
                    revision INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (owner, kind, id)
                );
                CREATE INDEX IF NOT EXISTS idx_documents_scope
                    ON documents(owner, kind, project_id, status, updated_at);
                CREATE INDEX IF NOT EXISTS idx_documents_session
                    ON documents(owner, kind, session_id, status, updated_at);
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner TEXT NOT NULL,
                    name TEXT NOT NULL,
                    entity_kind TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_owner_seq
                    ON events(owner, seq);
                """
            )
            # The first phase-0 schema keyed documents only by ``(kind,id)``.
            # Reads were owner-filtered, but two owners could not both register
            # a stable logical id such as ``tool://renderer``: the second write
            # collided with the first owner's row.  Migrate in place before a
            # feature flag is ever enabled on a multi-user installation.
            pk = [
                str(row["name"])
                for row in sorted(
                    db.execute("PRAGMA table_info(documents)").fetchall(),
                    key=lambda row: int(row["pk"] or 0),
                )
                if int(row["pk"] or 0) > 0
            ]
            if pk == ["kind", "id"]:
                db.executescript(
                    """
                    DROP TABLE IF EXISTS documents_owner_scoped;
                    CREATE TABLE documents_owner_scoped (
                        kind TEXT NOT NULL,
                        id TEXT NOT NULL,
                        owner TEXT NOT NULL,
                        project_id TEXT NOT NULL DEFAULT '',
                        session_id TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT '',
                        revision INTEGER NOT NULL DEFAULT 1,
                        payload TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (owner, kind, id)
                    );
                    INSERT INTO documents_owner_scoped
                        (kind,id,owner,project_id,session_id,status,revision,payload,created_at,updated_at)
                    SELECT kind,id,owner,project_id,session_id,status,revision,payload,created_at,updated_at
                    FROM documents;
                    DROP TABLE documents;
                    ALTER TABLE documents_owner_scoped RENAME TO documents;
                    CREATE INDEX idx_documents_scope
                        ON documents(owner, kind, project_id, status, updated_at);
                    CREATE INDEX idx_documents_session
                        ON documents(owner, kind, session_id, status, updated_at);
                    """
                )

    @staticmethod
    def _decode(row: sqlite3.Row) -> Dict[str, Any]:
        value = json.loads(str(row["payload"]))
        if not isinstance(value, dict):
            value = {"value": value}
        value.setdefault("id", str(row["id"]))
        value["revision"] = int(row["revision"])
        value.setdefault("created_at", str(row["created_at"]))
        value["updated_at"] = str(row["updated_at"])
        return value

    def put(self, kind: str, document: Mapping[str, Any], *, owner: str,
            project_id: str = "", session_id: str = "", status: str = "",
            expected_revision: Optional[int] = None) -> Dict[str, Any]:
        doc = dict(document)
        ident = str(doc.get("id") or "").strip()
        if not ident or not owner:
            raise StoreError("kind, id and owner are required")
        stamp = now_iso()
        with self._lock, self._connect() as db:
            # Acquire the write reservation before reading the revision.  A
            # plain SELECT followed by an UPSERT is only optimistic inside one
            # Python instance; two web workers could otherwise both observe
            # revision N and publish contradictory N+1 updates.  IMMEDIATE
            # serialises that compare-and-swap across processes as well.
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT revision, created_at FROM documents WHERE owner=? AND kind=? AND id=?",
                (owner, kind, ident),
            ).fetchone()
            current = int(row["revision"]) if row is not None else 0
            if expected_revision is not None and current != int(expected_revision):
                raise RevisionConflict(f"expected revision {expected_revision}, found {current}")
            revision = current + 1
            created = str(row["created_at"]) if row is not None else str(doc.get("created_at") or stamp)
            doc.update({"id": ident, "owner": owner, "project_id": project_id,
                        "session_id": session_id, "status": status,
                        "revision": revision, "created_at": created, "updated_at": stamp})
            raw = json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            db.execute(
                """INSERT INTO documents
                   (kind,id,owner,project_id,session_id,status,revision,payload,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(owner,kind,id) DO UPDATE SET
                     project_id=excluded.project_id, session_id=excluded.session_id,
                     status=excluded.status, revision=excluded.revision,
                     payload=excluded.payload, updated_at=excluded.updated_at""",
                (kind, ident, owner, project_id, session_id, status, revision, raw, created, stamp),
            )
        return doc

    def get(self, kind: str, ident: str, *, owner: str) -> Optional[Dict[str, Any]]:
        if not owner:
            return None
        try:
            with self._connect() as db:
                row = db.execute(
                    "SELECT * FROM documents WHERE kind=? AND id=? AND owner=?",
                    (kind, ident, owner),
                ).fetchone()
            return self._decode(row) if row is not None else None
        except Exception:
            logger.exception("durable store read failed: %s/%s", kind, ident)
            return None

    def list(self, kind: str, *, owner: str, project_id: str = "",
             session_id: str = "", status: str = "", limit: int = 100) -> List[Dict[str, Any]]:
        if not owner:
            return []
        clauses, args = ["kind=?", "owner=?"], [kind, owner]
        for column, value in (("project_id", project_id), ("session_id", session_id), ("status", status)):
            if value:
                clauses.append(f"{column}=?")
                args.append(value)
        args.append(max(1, min(1000, int(limit))))
        try:
            with self._connect() as db:
                rows = db.execute(
                    "SELECT * FROM documents WHERE " + " AND ".join(clauses)
                    + " ORDER BY updated_at DESC LIMIT ?", args,
                ).fetchall()
            return [self._decode(row) for row in rows]
        except Exception:
            logger.exception("durable store list failed: %s", kind)
            return []

    def list_all(self, kind: str, *, status: str = "", limit: int = 1000) -> List[Dict[str, Any]]:
        """Internal maintenance scan across owners.

        Deliberately separate from ``list`` so request paths cannot widen a
        caller's scope accidentally.  Used only for conservative boot recovery.
        """
        clauses, args = ["kind=?"], [kind]
        if status:
            clauses.append("status=?")
            args.append(status)
        args.append(max(1, min(5000, int(limit))))
        try:
            with self._connect() as db:
                rows = db.execute(
                    "SELECT * FROM documents WHERE " + " AND ".join(clauses)
                    + " ORDER BY updated_at ASC LIMIT ?", args,
                ).fetchall()
            return [self._decode(row) for row in rows]
        except Exception:
            logger.exception("durable store maintenance scan failed: %s", kind)
            return []

    def emit(self, *, owner: str, name: str, entity_kind: str,
             entity_id: str, payload: Mapping[str, Any]) -> int:
        if not owner:
            raise StoreError("owner is required")
        with self._lock, self._connect() as db:
            cursor = db.execute(
                "INSERT INTO events(owner,name,entity_kind,entity_id,payload,created_at) VALUES (?,?,?,?,?,?)",
                (owner, name, entity_kind, entity_id,
                 json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":")), now_iso()),
            )
            return int(cursor.lastrowid)

    def events(self, *, owner: str, since: int = 0, limit: int = 200,
               entity_id: str = "") -> List[Dict[str, Any]]:
        if not owner:
            return []
        clauses, args = ["owner=?", "seq>?"], [owner, max(0, int(since))]
        if entity_id:
            clauses.append("entity_id=?")
            args.append(entity_id)
        args.append(max(1, min(1000, int(limit))))
        try:
            with self._connect() as db:
                rows = db.execute(
                    "SELECT * FROM events WHERE " + " AND ".join(clauses)
                    + " ORDER BY seq ASC LIMIT ?", args,
                ).fetchall()
            output = []
            for row in rows:
                output.append({"seq": int(row["seq"]), "name": str(row["name"]),
                               "entity_kind": str(row["entity_kind"]),
                               "entity_id": str(row["entity_id"]),
                               "payload": json.loads(str(row["payload"])),
                               "created_at": str(row["created_at"])})
            return output
        except Exception:
            logger.exception("durable store event read failed")
            return []
