"""harness_evolution/store.py — sqlite persistence for the versioned harness.

One database file under `DATA_DIR` (or an explicit `db_path`, mainly for
tests), two tables (`harness_revisions`, `candidate_patches`). Every method
opens its own connection and closes it before returning — no long-lived
handle, no `.db-journal` left mid-write, and safe under Windows where an
open handle blocks the next process from touching the file.

The CAS step A30 depends on (`set_active`) is implemented as a single
`UPDATE ... WHERE revision_id = ? AND status = 'active' AND version = ?` —
SQLite's own write lock (`BEGIN IMMEDIATE`) serializes two concurrent callers
so the loser's `UPDATE` really does see the winner's committed row and
really does affect zero rows, never a lost update from two writers reading
the same "current" version before either commits.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator, List, Optional

from src.constants import DATA_DIR
from .models import CandidatePatch, HarnessRevision, new_id

DEFAULT_DB_PATH = os.path.join(DATA_DIR, "harness_evolution.db")

#: How long a writer waits on SQLite's own file lock before giving up. A
#: concurrent CAS (A30) is expected to briefly block, not fail outright.
_BUSY_TIMEOUT_S = 30


class StaleParent(RuntimeError):
    """`set_active`/`promote` refused: the revision a patch targeted is no
    longer the active one (or not at the version the caller expected) —
    someone else's promotion (or edit) landed first. Carries the CURRENT
    active revision so a caller can rebase against it instead of guessing."""

    def __init__(self, current: HarnessRevision) -> None:
        super().__init__(
            f"revision {current.revision_id!r} is no longer at the expected "
            f"version (now v{current.version}, status={current.status!r})")
        self.current = current


class HarnessEvolutionStore:
    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = db_path or DEFAULT_DB_PATH
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        self._init_lock = threading.Lock()
        self._ensure_schema()

    @contextmanager
    def _conn(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=_BUSY_TIMEOUT_S,
                               isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            if immediate:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            if immediate:
                conn.execute("COMMIT")
        except Exception:
            if immediate:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        with self._init_lock, self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS harness_revisions (
                    revision_id TEXT PRIMARY KEY,
                    parent_id TEXT,
                    created_at TEXT NOT NULL,
                    refs_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS candidate_patches (
                    patch_id TEXT PRIMARY KEY,
                    parent_revision TEXT NOT NULL,
                    changes_json TEXT NOT NULL,
                    source_trace_ids_json TEXT NOT NULL,
                    scope TEXT,
                    required_capabilities_json TEXT NOT NULL,
                    evaluation_json TEXT NOT NULL,
                    rollback_target TEXT,
                    status TEXT NOT NULL,
                    trace TEXT,
                    created_at TEXT NOT NULL,
                    promoted_revision_id TEXT
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_revisions_status ON harness_revisions(status)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_patches_parent ON candidate_patches(parent_revision)")
            # Every store starts with exactly one active revision so a
            # caller never has to special-case "no harness yet".
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM harness_revisions").fetchone()
            if int(row["n"]) == 0:
                bootstrap = HarnessRevision.bootstrap()
                conn.execute(
                    "INSERT INTO harness_revisions "
                    "(revision_id, parent_id, created_at, refs_json, status, version) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (bootstrap.revision_id, bootstrap.parent_id, bootstrap.created_at,
                     json.dumps(bootstrap.refs), bootstrap.status, bootstrap.version))

    # ── revisions ────────────────────────────────────────────────────────

    def get_revision(self, revision_id: str) -> Optional[HarnessRevision]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM harness_revisions WHERE revision_id = ?",
                (revision_id,)).fetchone()
            return HarnessRevision.from_row(row) if row else None

    def active_revision(self) -> HarnessRevision:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM harness_revisions WHERE status = 'active' "
                "ORDER BY created_at DESC LIMIT 1").fetchone()
            if row is None:
                raise RuntimeError("harness_evolution store has no active revision")
            return HarnessRevision.from_row(row)

    def list_revisions(self) -> List[HarnessRevision]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM harness_revisions ORDER BY created_at ASC").fetchall()
            return [HarnessRevision.from_row(r) for r in rows]

    def insert_revision(self, revision: HarnessRevision) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO harness_revisions "
                "(revision_id, parent_id, created_at, refs_json, status, version) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (revision.revision_id, revision.parent_id, revision.created_at,
                 json.dumps(revision.refs), revision.status, revision.version))

    def retire_revision(self, revision_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE harness_revisions SET status = 'retired' WHERE revision_id = ?",
                (revision_id,))

    def set_active(self, revision_id: str) -> None:
        """Force an already-inserted revision to `active`, unconditionally —
        used by rollback/recheck, which have already decided (this is not
        the CAS path; see `promote_cas`)."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE harness_revisions SET status = 'active' WHERE revision_id = ?",
                (revision_id,))

    def promote_cas(self, *, parent_revision_id: str,
                    new_revision: HarnessRevision) -> HarnessRevision:
        """A28/A30's transaction: atomically retire `parent_revision_id`
        (only if it is STILL the active revision) and insert `new_revision`
        as the new active one. `BEGIN IMMEDIATE` takes SQLite's write lock
        for the whole check-then-act, so two threads racing this call are
        strictly ordered by the database, not by Python — the loser's
        `WHERE status = 'active'` genuinely sees zero rows once the winner
        has committed, and raises `StaleParent` with the revision that won
        instead of silently doing nothing or clobbering it.
        """
        with self._conn(immediate=True) as conn:
            row = conn.execute(
                "SELECT * FROM harness_revisions WHERE revision_id = ?",
                (parent_revision_id,)).fetchone()
            if row is None or row["status"] != "active":
                current_row = conn.execute(
                    "SELECT * FROM harness_revisions WHERE status = 'active' "
                    "ORDER BY created_at DESC LIMIT 1").fetchone()
                current = (HarnessRevision.from_row(current_row) if current_row
                           else HarnessRevision.from_row(row))
                raise StaleParent(current)
            cur = conn.execute(
                "UPDATE harness_revisions SET status = 'retired' "
                "WHERE revision_id = ? AND status = 'active'",
                (parent_revision_id,))
            if cur.rowcount != 1:
                current_row = conn.execute(
                    "SELECT * FROM harness_revisions WHERE status = 'active' "
                    "ORDER BY created_at DESC LIMIT 1").fetchone()
                raise StaleParent(HarnessRevision.from_row(current_row))
            conn.execute(
                "INSERT INTO harness_revisions "
                "(revision_id, parent_id, created_at, refs_json, status, version) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (new_revision.revision_id, new_revision.parent_id, new_revision.created_at,
                 json.dumps(new_revision.refs), "active", new_revision.version))
            return new_revision

    # ── candidate patches ───────────────────────────────────────────────

    def new_patch_id(self) -> str:
        return new_id("cand")

    def insert_patch(self, patch: CandidatePatch) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO candidate_patches "
                "(patch_id, parent_revision, changes_json, source_trace_ids_json, scope, "
                " required_capabilities_json, evaluation_json, rollback_target, status, "
                " trace, created_at, promoted_revision_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (patch.patch_id, patch.parent_revision, json.dumps(patch.changes),
                 json.dumps(patch.source_trace_ids), patch.scope,
                 json.dumps(patch.required_capabilities), json.dumps(patch.evaluation),
                 patch.rollback_target, patch.status, patch.trace, patch.created_at,
                 patch.promoted_revision_id))

    def get_patch(self, patch_id: str) -> Optional[CandidatePatch]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM candidate_patches WHERE patch_id = ?",
                (patch_id,)).fetchone()
            return CandidatePatch.from_row(row) if row else None

    def list_patches(self) -> List[CandidatePatch]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM candidate_patches ORDER BY created_at ASC").fetchall()
            return [CandidatePatch.from_row(r) for r in rows]

    def update_patch(self, patch: CandidatePatch) -> None:
        """Persists every mutable field, INCLUDING `parent_revision` — A30's
        `rebase()` repoints a stale patch at the revision that won, and that
        new parent has to survive this write or a retried promotion would
        silently re-target the revision that already lost the race."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE candidate_patches SET parent_revision = ?, changes_json = ?, "
                "source_trace_ids_json = ?, scope = ?, required_capabilities_json = ?, "
                "evaluation_json = ?, rollback_target = ?, status = ?, trace = ?, "
                "promoted_revision_id = ? WHERE patch_id = ?",
                (patch.parent_revision, json.dumps(patch.changes),
                 json.dumps(patch.source_trace_ids), patch.scope,
                 json.dumps(patch.required_capabilities), json.dumps(patch.evaluation),
                 patch.rollback_target, patch.status, patch.trace, patch.promoted_revision_id,
                 patch.patch_id))
