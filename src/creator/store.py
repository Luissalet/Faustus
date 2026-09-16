"""DocumentStore — sqlite-backed CAS store for CreatorDocument (WP02).

New persistence (CONTRATO.md rule 2): ``DATA_DIR/creator/creator_documents.sqlite3``,
one connection opened and closed per call, WAL, optimistic concurrency via a
real ``BEGIN IMMEDIATE`` transaction — no in-process lock as the authority,
same pattern as ``src/harness_evolution/store.py`` and ``src/budget_account.py``.

Three tables:

* ``creator_documents`` — current state, one row per document (id, project_id,
  owner, kind, schema_version, revision, state, content, asset_refs).
* ``creator_document_revisions`` — append-only snapshot per revision, so a
  deduplicated command replay and ``history()`` can both reconstruct exactly
  what was returned the first time, without reinterpreting later data.
* ``creator_document_commands`` — append-only, ``PRIMARY KEY (doc_id,
  command_id)``. A command is inserted ONLY on a successful apply; a 409
  conflict writes nothing, so retrying the same ``command_id`` after fixing
  ``expected_revision`` is not permanently deduplicated to the failed attempt.

Owner is a plain equality filter baked into every SELECT/UPDATE, never
checked after the fact: a foreign owner's document id behaves exactly like
an unknown one (``DocumentNotFound``), per CONTRATO.md rule 3.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

from .documents import (
    DOCUMENT_KINDS,
    DOCUMENT_STATES,
    SCHEMA_VERSION,
    CreatorDocument,
    new_document_id,
    validate_content,
)
from .errors import DocumentNotFound, InvalidDocument, InvalidOperation, RevisionConflict
from .ops import registry as _ops_registry

_BUSY_TIMEOUT_S = 30


def default_path() -> str:
    from src.constants import DATA_DIR
    return os.path.join(DATA_DIR, "creator", "creator_documents.sqlite3")


def _row_to_document(row: sqlite3.Row) -> CreatorDocument:
    return CreatorDocument(
        id=row["id"],
        project_id=row["project_id"],
        owner=row["owner"],
        kind=row["kind"],
        schema_version=row["schema_version"],
        revision=row["revision"],
        content=json.loads(row["content_json"]),
        state=row["state"],
        asset_refs=json.loads(row["asset_refs_json"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class DocumentStore:
    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = db_path or default_path()
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        self._init_lock = threading.Lock()
        self._ensure_schema()

    # ------------------------------------------------------------------
    # Connection / schema
    # ------------------------------------------------------------------

    @contextmanager
    def _conn(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=_BUSY_TIMEOUT_S, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys = ON")
            if immediate:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            if immediate:
                conn.execute("COMMIT")
        except Exception:
            if immediate:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        with self._init_lock, self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS creator_documents (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    schema_version INTEGER NOT NULL,
                    revision INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    content_json TEXT NOT NULL,
                    asset_refs_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_creator_documents_owner_project "
                "ON creator_documents(owner, project_id)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS creator_document_revisions (
                    doc_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    content_json TEXT NOT NULL,
                    asset_refs_json TEXT NOT NULL,
                    at REAL NOT NULL,
                    PRIMARY KEY (doc_id, revision)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS creator_document_commands (
                    doc_id TEXT NOT NULL,
                    command_id TEXT NOT NULL,
                    expected_revision INTEGER NOT NULL,
                    op_json TEXT NOT NULL,
                    result_revision INTEGER NOT NULL,
                    at REAL NOT NULL,
                    PRIMARY KEY (doc_id, command_id)
                )
                """
            )

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get(self, owner: str, doc_id: str) -> Optional[CreatorDocument]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM creator_documents WHERE id = ? AND owner = ?",
                (doc_id, owner or ""),
            ).fetchone()
            if row is None:
                return None
            return _row_to_document(row)

    def list_for_project(self, owner: str, project_id: str) -> List[CreatorDocument]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM creator_documents WHERE owner = ? AND project_id = ? "
                "ORDER BY updated_at DESC",
                (owner or "", project_id),
            ).fetchall()
            return [_row_to_document(r) for r in rows]

    def history(self, owner: str, doc_id: str) -> List[Dict[str, Any]]:
        """Append-only command log for a document the caller owns.

        Empty list (not an error) for an unknown/foreign document — callers
        that already resolved the document via :meth:`get` treat that as the
        404 case; this stays a pure read so it composes with either.
        """
        with self._conn() as conn:
            owned = conn.execute(
                "SELECT 1 FROM creator_documents WHERE id = ? AND owner = ?",
                (doc_id, owner or ""),
            ).fetchone()
            if owned is None:
                return []
            rows = conn.execute(
                "SELECT command_id, expected_revision, op_json, result_revision, at "
                "FROM creator_document_commands WHERE doc_id = ? ORDER BY result_revision ASC",
                (doc_id,),
            ).fetchall()
            return [
                {
                    "command_id": r["command_id"],
                    "expected_revision": r["expected_revision"],
                    "op": json.loads(r["op_json"]),
                    "result_revision": r["result_revision"],
                    "at": r["at"],
                }
                for r in rows
            ]

    def get_revision_snapshot(self, owner: str, doc_id: str, revision: int) -> Optional[Dict[str, Any]]:
        """WP12 addition: the exact ``content``/``state``/``asset_refs`` the
        document had at a past ``revision``, owner-scoped. Backs
        ``src/creator/ops/undo.py``'s ``undo_to`` — undo restores CONTENT
        from this append-only snapshot log, it never re-derives it by
        replaying/reversing ops. Returns ``None`` for an unknown/foreign
        document OR an unknown revision, same 404-shaped treatment as
        :meth:`get` (CONTRATO.md rule 3).
        """
        with self._conn() as conn:
            owned = conn.execute(
                "SELECT 1 FROM creator_documents WHERE id = ? AND owner = ?",
                (doc_id, owner or ""),
            ).fetchone()
            if owned is None:
                return None
            row = conn.execute(
                "SELECT * FROM creator_document_revisions WHERE doc_id = ? AND revision = ?",
                (doc_id, int(revision)),
            ).fetchone()
            if row is None:
                return None
            return {
                "revision": row["revision"],
                "state": row["state"],
                "content": json.loads(row["content_json"]),
                "asset_refs": json.loads(row["asset_refs_json"]),
                "at": row["at"],
            }

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def create(
        self,
        owner: str,
        project_id: str,
        kind: str,
        content: Dict[str, Any],
        *,
        state: str = "proposed",
        asset_refs: Optional[List[str]] = None,
    ) -> CreatorDocument:
        if kind not in DOCUMENT_KINDS:
            raise InvalidDocument(f"unknown document kind: {kind!r}")
        if state not in DOCUMENT_STATES:
            raise InvalidDocument(f"unknown document state: {state!r}")
        if not project_id:
            raise InvalidDocument("project_id is required")
        validate_content(kind, content)
        asset_refs = list(asset_refs or [])
        doc_id = new_document_id()
        stamp = time.time()
        content_json = json.dumps(content)
        asset_refs_json = json.dumps(asset_refs)
        with self._conn(immediate=True) as conn:
            conn.execute(
                """
                INSERT INTO creator_documents
                    (id, project_id, owner, kind, schema_version, revision, state,
                     content_json, asset_refs_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
                """,
                (doc_id, project_id, owner or "", kind, SCHEMA_VERSION, state,
                 content_json, asset_refs_json, stamp, stamp),
            )
            conn.execute(
                """
                INSERT INTO creator_document_revisions
                    (doc_id, revision, state, content_json, asset_refs_json, at)
                VALUES (?, 1, ?, ?, ?, ?)
                """,
                (doc_id, state, content_json, asset_refs_json, stamp),
            )
        return CreatorDocument(
            id=doc_id, project_id=project_id, owner=owner or "", kind=kind,
            schema_version=SCHEMA_VERSION, revision=1, content=content, state=state,
            asset_refs=asset_refs, created_at=stamp, updated_at=stamp,
        )

    def apply_command(
        self,
        owner: str,
        doc_id: str,
        command_id: str,
        expected_revision: int,
        op: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Apply a typed operation under optimistic concurrency.

        Returns ``{"doc": CreatorDocument, "applied": bool, "deduped": bool}``.
        Raises :class:`DocumentNotFound` (unknown/foreign id, checked and
        reported identically), :class:`RevisionConflict` (``current_revision``
        on the exception) or :class:`InvalidOperation`/:class:`InvalidDocument`
        for a malformed op or the content it would produce.
        """
        if not command_id:
            raise InvalidOperation("command_id is required")
        with self._conn(immediate=True) as conn:
            row = conn.execute(
                "SELECT * FROM creator_documents WHERE id = ? AND owner = ?",
                (doc_id, owner or ""),
            ).fetchone()
            if row is None:
                raise DocumentNotFound(doc_id)

            existing_cmd = conn.execute(
                "SELECT result_revision FROM creator_document_commands "
                "WHERE doc_id = ? AND command_id = ?",
                (doc_id, command_id),
            ).fetchone()
            if existing_cmd is not None:
                snap = conn.execute(
                    "SELECT * FROM creator_document_revisions WHERE doc_id = ? AND revision = ?",
                    (doc_id, existing_cmd["result_revision"]),
                ).fetchone()
                doc = CreatorDocument(
                    id=doc_id, project_id=row["project_id"], owner=row["owner"],
                    kind=row["kind"], schema_version=row["schema_version"],
                    revision=snap["revision"], content=json.loads(snap["content_json"]),
                    state=snap["state"], asset_refs=json.loads(snap["asset_refs_json"]),
                    created_at=row["created_at"], updated_at=snap["at"],
                )
                return {"doc": doc, "applied": True, "deduped": True}

            current_revision = int(row["revision"])
            if int(expected_revision) != current_revision:
                raise RevisionConflict(current_revision)

            content = json.loads(row["content_json"])
            state = row["state"]
            asset_refs = json.loads(row["asset_refs_json"])
            content, state, asset_refs = _apply_op(
                row["kind"], content, state, asset_refs, op, revision=current_revision,
            )

            new_revision = current_revision + 1
            stamp = time.time()
            content_json = json.dumps(content)
            asset_refs_json = json.dumps(asset_refs)
            conn.execute(
                "UPDATE creator_documents SET revision = ?, state = ?, content_json = ?, "
                "asset_refs_json = ?, updated_at = ? WHERE id = ? AND owner = ?",
                (new_revision, state, content_json, asset_refs_json, stamp, doc_id, owner or ""),
            )
            conn.execute(
                "INSERT INTO creator_document_revisions "
                "(doc_id, revision, state, content_json, asset_refs_json, at) VALUES (?, ?, ?, ?, ?, ?)",
                (doc_id, new_revision, state, content_json, asset_refs_json, stamp),
            )
            conn.execute(
                "INSERT INTO creator_document_commands "
                "(doc_id, command_id, expected_revision, op_json, result_revision, at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (doc_id, command_id, int(expected_revision), json.dumps(op), new_revision, stamp),
            )
            doc = CreatorDocument(
                id=doc_id, project_id=row["project_id"], owner=row["owner"], kind=row["kind"],
                schema_version=row["schema_version"], revision=new_revision, content=content,
                state=state, asset_refs=asset_refs, created_at=row["created_at"], updated_at=stamp,
            )
            return {"doc": doc, "applied": True, "deduped": False}


def _apply_op(
    kind: str,
    content: Dict[str, Any],
    state: str,
    asset_refs: List[str],
    op: Dict[str, Any],
    *,
    revision: Optional[int] = None,
) -> tuple:
    """WP12 aditive hook: ``op["type"]`` is first looked up in the typed ops
    registry (``src/creator/ops/registry.py`` — canvas/timeline/transcript/
    undo ops, discovered by ``pkgutil``). Only when that registry has no
    handler for the type does this fall through to the small set of
    generic ops WP02 shipped (``set_content``/``patch_content``/
    ``set_state``/``set_asset_refs``) below. This is the single dispatcher
    change this file makes for WP12; nothing about the generic ops'
    existing behaviour changed.
    """
    if not isinstance(op, dict) or "type" not in op:
        raise InvalidOperation("op must be an object with a 'type'")
    op_type = op["type"]

    if isinstance(op_type, str) and _ops_registry.get(op_type) is not None:
        doc_snapshot = {
            "kind": kind, "content": content, "state": state,
            "asset_refs": asset_refs, "revision": revision,
        }
        new_content = _ops_registry.apply(doc_snapshot, op)
        validate_content(kind, new_content)
        return new_content, state, asset_refs

    if op_type == "set_content":
        new_content = op.get("content")
        if not isinstance(new_content, dict):
            raise InvalidOperation("set_content requires an object 'content'")
        validate_content(kind, new_content)
        return new_content, state, asset_refs

    if op_type == "patch_content":
        patch = op.get("patch")
        if not isinstance(patch, dict):
            raise InvalidOperation("patch_content requires an object 'patch'")
        merged = dict(content)
        merged.update(patch)
        validate_content(kind, merged)
        return merged, state, asset_refs

    if op_type == "set_state":
        new_state = op.get("state")
        if new_state not in DOCUMENT_STATES:
            raise InvalidOperation(f"set_state requires state in {DOCUMENT_STATES}")
        return content, new_state, asset_refs

    if op_type == "set_asset_refs":
        refs = op.get("asset_refs")
        if not isinstance(refs, list) or not all(isinstance(r, str) for r in refs):
            raise InvalidOperation("set_asset_refs requires a list of strings")
        return content, state, list(refs)

    raise InvalidOperation(f"unknown op type: {op_type!r}")


_store: Optional[DocumentStore] = None
_store_lock = threading.Lock()


def get_store() -> DocumentStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = DocumentStore()
    return _store
