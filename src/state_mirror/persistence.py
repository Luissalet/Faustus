"""Where observations and materialised state live: one SQLite file, WAL, ours.

Its own database for a reason the other two do not share. `context_engine.db`
exists so a derived index cannot lock `app.db`; `council.db` exists so an
hour-long deliberation cannot hold a write lock a chat turn is waiting on.
This one exists because of the WRITE RATE: probes append several observations a
second while work is running, and every one is an insert. Putting that traffic
in a file a user's sessions also live in would be a design decision nobody made
on purpose.

Nothing in here is canonical. Losing this file costs a rebuild from the sources
that already own the facts; it cannot cost a fact. That is the licence for the
retention policy below (`compact()` drops old observations without ceremony)
and it is the reason quarantine-on-corruption is the right response to a
damaged file rather than a crash.

The asymmetry the rest of the package relies on, same as `council/persistence`:
**reads never raise** -- they answer `None`, `[]` or `{}` and log -- and
**writes raise typed errors** that a route maps to a refusal. A read on the
turn path that could throw would make the mirror able to break the thing it is
supposed to describe.

Three invariants live in SQLite rather than in Python, because a rule enforced
in application code is a rule two processes can race through:

* one materialised state per entity (`PRIMARY KEY (entity_id)`);
* one row per observation identity (`idx_state_obs_identity`), which is what
  makes a replayed event, a retried webhook and a reconnecting subscription
  collapse into one observation instead of three;
* one live relation per (from, kind, to) (`idx_state_relation_live`), so an
  edge cannot exist twice with two different origins.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import threading
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from src.constants import STATE_MIRROR_DB
from src.contracts.base import now_iso
from src.state_mirror.contracts import (
    MaterializedState,
    StateConflict,
    StateEntity,
    StateError,
    StateObservation,
    StateRelation,
)
from src.state_mirror.replay import SCHEMA as _REPLAY_SCHEMA

logger = logging.getLogger(__name__)

__all__ = [
    "StateStoreError",
    "NotFound",
    "ANY_OWNER",
    "StateStore",
    "register_schema",
    "registered_schemas",
    "db_path",
    "use_path",
    "store",
    "now_iso",
]


class StateStoreError(StateError):
    """A write this store refused. Reads never raise one."""


class NotFound(StateStoreError):
    def __init__(self, kind: str, row_id: str) -> None:
        super().__init__(f"{kind}.id", "no such row here", got=row_id)


#: What a sweep passes as `owner` when it means every owner. `None` rather than
#: `""`, because `""` is a real owner on a no-login install and the two must
#: never collapse. A ROUTE never passes this: `require_admin` plus a resolved
#: owner is what keeps one user's state out of another's answer.
ANY_OWNER: Optional[str] = None

_LOCK = threading.RLock()
_SCHEMAS: Dict[str, Tuple[str, ...]] = {}
_PATH_OVERRIDE: Optional[str] = None
_STORE: Optional["StateStore"] = None


def register_schema(name: str, statements: Sequence[str]) -> None:
    """Add DDL a feature needs, applied on every connection.

    Per-feature rather than one blob so that a broken statement costs its own
    table and not the store: `_apply_schema` logs and carries on.
    """
    with _LOCK:
        _SCHEMAS[str(name)] = tuple(str(s) for s in statements)


def registered_schemas() -> Tuple[str, ...]:
    with _LOCK:
        return tuple(sorted(_SCHEMAS))


def db_path() -> str:
    return _PATH_OVERRIDE or STATE_MIRROR_DB


def use_path(path: Optional[str]) -> None:
    """Point the store at another file. Tests and the doctor only.

    Also drops the memoised store, because a process holding a connection to
    the old file would keep answering from it.
    """
    global _PATH_OVERRIDE, _STORE
    with _LOCK:
        _PATH_OVERRIDE = str(path) if path else None
        _STORE = None


def store() -> "StateStore":
    """The process-wide store, built on first use."""
    global _STORE
    with _LOCK:
        if _STORE is None:
            _STORE = StateStore()
        return _STORE


_CORE_SCHEMA: Tuple[str, ...] = _REPLAY_SCHEMA + (
    """
    CREATE TABLE IF NOT EXISTS state_entities (
        id            TEXT PRIMARY KEY,
        kind          TEXT NOT NULL,
        owner         TEXT NOT NULL DEFAULT '',
        namespace     TEXT NOT NULL DEFAULT 'real',
        project_id    TEXT NOT NULL DEFAULT '',
        display_name  TEXT NOT NULL DEFAULT '',
        labels        TEXT NOT NULL DEFAULT '[]',
        source_refs   TEXT NOT NULL DEFAULT '[]',
        schema        TEXT NOT NULL DEFAULT '',
        created_at    TEXT NOT NULL DEFAULT '',
        updated_at    TEXT NOT NULL DEFAULT '',
        retired_at    TEXT NOT NULL DEFAULT '',
        sensitivity   TEXT NOT NULL DEFAULT 'private',
        schema_version INTEGER NOT NULL DEFAULT 1
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_state_entity_scope "
    "ON state_entities (owner, namespace, kind)",
    "CREATE INDEX IF NOT EXISTS idx_state_entity_project "
    "ON state_entities (project_id, kind)",
    """
    CREATE TABLE IF NOT EXISTS state_observations (
        id            TEXT PRIMARY KEY,
        identity      TEXT NOT NULL,
        entity_id     TEXT NOT NULL,
        owner         TEXT NOT NULL DEFAULT '',
        namespace     TEXT NOT NULL DEFAULT 'real',
        project_id    TEXT NOT NULL DEFAULT '',
        schema        TEXT NOT NULL DEFAULT '',
        source        TEXT NOT NULL DEFAULT '',
        epistemic     TEXT NOT NULL DEFAULT 'observed',
        sequence      INTEGER NOT NULL DEFAULT 0,
        observed_at   TEXT NOT NULL DEFAULT '',
        received_at   TEXT NOT NULL DEFAULT '',
        valid_for_seconds INTEGER NOT NULL DEFAULT 0,
        partial       INTEGER NOT NULL DEFAULT 1,
        state         TEXT NOT NULL DEFAULT '{}',
        evidence_refs TEXT NOT NULL DEFAULT '[]',
        source_revision TEXT NOT NULL DEFAULT '',
        sensitivity   TEXT NOT NULL DEFAULT 'private',
        schema_version INTEGER NOT NULL DEFAULT 1
    )
    """,
    # THE dedupe invariant. Section 8.1 asks for deduplication; doing it in
    # Python would leave two processes free to insert the same observation at
    # the same moment, and the whole point of an append-only log is that
    # reading it twice tells you the same thing.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_state_obs_identity "
    "ON state_observations (identity)",
    "CREATE INDEX IF NOT EXISTS idx_state_obs_entity "
    "ON state_observations (entity_id, observed_at)",
    "CREATE INDEX IF NOT EXISTS idx_state_obs_received "
    "ON state_observations (received_at)",
    "CREATE INDEX IF NOT EXISTS idx_state_obs_source "
    "ON state_observations (source, received_at)",
    """
    CREATE TABLE IF NOT EXISTS state_materialized (
        entity_id     TEXT PRIMARY KEY,
        owner         TEXT NOT NULL DEFAULT '',
        namespace     TEXT NOT NULL DEFAULT 'real',
        project_id    TEXT NOT NULL DEFAULT '',
        schema        TEXT NOT NULL DEFAULT '',
        revision      INTEGER NOT NULL DEFAULT 0,
        fields        TEXT NOT NULL DEFAULT '{}',
        conflicts     TEXT NOT NULL DEFAULT '[]',
        updated_at    TEXT NOT NULL DEFAULT '',
        changed_seq   INTEGER NOT NULL DEFAULT 0,
        schema_version INTEGER NOT NULL DEFAULT 1
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_state_mat_scope "
    "ON state_materialized (owner, namespace)",
    # The cursor `changed_since()` reads. Monotonic across the whole store, not
    # per entity, so one cursor can follow everything a consumer may see.
    "CREATE INDEX IF NOT EXISTS idx_state_mat_changed "
    "ON state_materialized (changed_seq)",
    """
    CREATE TABLE IF NOT EXISTS state_relations (
        id            TEXT PRIMARY KEY,
        rel_key       TEXT NOT NULL,
        from_id       TEXT NOT NULL,
        to_id         TEXT NOT NULL,
        kind          TEXT NOT NULL,
        owner         TEXT NOT NULL DEFAULT '',
        namespace     TEXT NOT NULL DEFAULT 'real',
        origin        TEXT NOT NULL DEFAULT 'observed',
        source        TEXT NOT NULL DEFAULT '',
        observed_at   TEXT NOT NULL DEFAULT '',
        created_at    TEXT NOT NULL DEFAULT '',
        retired_at    TEXT NOT NULL DEFAULT '',
        schema_version INTEGER NOT NULL DEFAULT 1
    )
    """,
    # One LIVE edge per (from, kind, to). Retired rows are excluded from the
    # index so history survives while the present stays unambiguous.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_state_relation_live "
    "ON state_relations (rel_key) WHERE retired_at = ''",
    "CREATE INDEX IF NOT EXISTS idx_state_relation_from ON state_relations (from_id)",
    "CREATE INDEX IF NOT EXISTS idx_state_relation_to ON state_relations (to_id)",
    """
    CREATE TABLE IF NOT EXISTS state_conflicts (
        id            TEXT PRIMARY KEY,
        conflict_key  TEXT NOT NULL,
        entity_id     TEXT NOT NULL,
        field         TEXT NOT NULL,
        owner         TEXT NOT NULL DEFAULT '',
        namespace     TEXT NOT NULL DEFAULT 'real',
        status        TEXT NOT NULL DEFAULT 'reconciling',
        claims        TEXT NOT NULL DEFAULT '[]',
        next_check    TEXT NOT NULL DEFAULT '',
        detected_at   TEXT NOT NULL DEFAULT '',
        resolved_at   TEXT NOT NULL DEFAULT '',
        resolution    TEXT NOT NULL DEFAULT '',
        schema_version INTEGER NOT NULL DEFAULT 1
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_state_conflict_live "
    "ON state_conflicts (conflict_key) WHERE status = 'reconciling'",
    "CREATE INDEX IF NOT EXISTS idx_state_conflict_entity "
    "ON state_conflicts (entity_id, status)",
    """
    CREATE TABLE IF NOT EXISTS state_source_cursors (
        source        TEXT NOT NULL,
        scope         TEXT NOT NULL DEFAULT '',
        cursor        TEXT NOT NULL DEFAULT '',
        health        TEXT NOT NULL DEFAULT 'unknown',
        detail        TEXT NOT NULL DEFAULT '',
        observations  INTEGER NOT NULL DEFAULT 0,
        failures      INTEGER NOT NULL DEFAULT 0,
        last_ok_at    TEXT NOT NULL DEFAULT '',
        last_error_at TEXT NOT NULL DEFAULT '',
        updated_at    TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (source, scope)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS state_sequence (
        name  TEXT PRIMARY KEY,
        value INTEGER NOT NULL DEFAULT 0
    )
    """,
)

register_schema("core", _CORE_SCHEMA)


def _quarantine(path: str, reason: Any) -> None:
    """Move a damaged file aside so the next open starts clean.

    The right response BECAUSE nothing here is canonical: an unreadable
    `state_mirror.db` costs a rebuild from the sources, and refusing to start
    would cost the whole application. The `.corrupt` copy is kept rather than
    deleted -- somebody may want to know what happened.
    """
    for suffix in ("", "-wal", "-shm"):
        target = f"{path}{suffix}"
        if not os.path.exists(target):
            continue
        try:
            os.replace(target, f"{target}.corrupt")
        except OSError as exc:
            logger.warning("state mirror: could not quarantine %s: %s", target, exc)
    logger.error("state mirror: %s was unreadable (%s); it has been moved aside "
                 "and a new one will be built from the sources", path, reason)


def _apply_schema(conn: sqlite3.Connection) -> None:
    with _LOCK:
        registered = dict(_SCHEMAS)
    for name, statements in registered.items():
        for statement in statements:
            try:
                conn.execute(statement)
            except sqlite3.Error as exc:
                # One bad DDL costs its own table, never the store. A feature
                # whose table did not appear degrades; the rest keeps working.
                logger.warning("state mirror: schema %s failed (%s)", name, exc)


def _connect(path: str) -> sqlite3.Connection:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    for pragma in ("PRAGMA journal_mode=WAL",
                   "PRAGMA synchronous=NORMAL",
                   "PRAGMA foreign_keys=ON"):
        with contextlib.suppress(sqlite3.Error):
            conn.execute(pragma)
    # OUTSIDE the suppression, and that is the whole point: sqlite opens a
    # non-database file happily and only complains when asked to read it. The
    # council's store learned this the same way -- without this probe, a
    # corrupt file "opens successfully" and quarantine never fires.
    #
    # And the handle is CLOSED before the exception leaves, which is not
    # tidiness: `_open` responds to this failure by quarantining the file with
    # `os.replace`, and on Windows a rename of a file still held open fails
    # with WinError 32. Leaking the handle here therefore degrades quarantine
    # into a logged warning, leaves the corrupt file exactly where it was, and
    # makes every subsequent open fail the same way forever. The Delta Engine's
    # store hit this on its first quarantine test; the same fix belongs here.
    try:
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        _apply_schema(conn)
        conn.commit()
    except Exception:
        with contextlib.suppress(Exception):
            conn.close()
        raise
    return conn


def _dumps(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError) as exc:
        logger.warning("state mirror: a value would not serialise (%s); stored "
                       "as null so the row survives", exc)
        return "null"


def _loads(raw: Any, default: Any = None) -> Any:
    if raw is None or raw == "":
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _kept(rows: Iterable[Any]) -> List[Any]:
    """Drop the rows that would not parse.

    A single unparseable row -- written by a newer build, or damaged -- must
    not make a whole list unreadable. The row that was skipped is already in
    the log with an exception beside it, and it is still in the file.
    """
    return [row for row in rows if row is not None]


class StateStore:
    """Observations, materialised state, relations, conflicts and cursors.

    One connection per instance, guarded by an `RLock`, in the same shape as
    `council.persistence.CouncilStore`. Reads answer empty and log; writes
    raise `StateStoreError`.
    """

    def __init__(self, *, path: Optional[str] = None) -> None:
        self._path = str(path) if path else ""
        self._conn: Optional[sqlite3.Connection] = None
        self._guard = threading.RLock()

    # -- plumbing ---------------------------------------------------------

    def path(self) -> str:
        return self._path or db_path()

    def _open(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        target = self.path()
        try:
            self._conn = _connect(target)
        except sqlite3.DatabaseError as exc:
            _quarantine(target, exc)
            try:
                self._conn = _connect(target)
            except sqlite3.DatabaseError as second:
                raise StateStoreError("store", f"{target} cannot be opened",
                                      got=str(second)) from second
        return self._conn

    @contextlib.contextmanager
    def _db(self):
        with self._guard:
            conn = self._open()
            try:
                yield conn
                conn.commit()
            except Exception:
                with contextlib.suppress(sqlite3.Error):
                    conn.rollback()
                raise

    def close(self) -> None:
        with self._guard:
            if self._conn is not None:
                with contextlib.suppress(sqlite3.Error):
                    self._conn.close()
                self._conn = None

    def _next_seq(self, conn: sqlite3.Connection, name: str = "changed") -> int:
        """The store-wide change cursor. Monotonic, gapless enough to follow.

        One counter for everything rather than one per entity, because a
        consumer following changes wants a single number to hold: "give me
        everything after 812" has to mean the same thing for a run and for a
        service or the cursor is useless.
        """
        conn.execute(
            "INSERT INTO state_sequence (name, value) VALUES (?, 1) "
            "ON CONFLICT(name) DO UPDATE SET value = value + 1", [name])
        row = conn.execute("SELECT value FROM state_sequence WHERE name = ?",
                           [name]).fetchone()
        return int(row["value"]) if row else 0

    @staticmethod
    def _scope(owner: Any = "", namespace: str = "", project_id: str = "",
               *, alias: str = "") -> Tuple[str, List[Any]]:
        """A WHERE fragment for owner/namespace/project.

        An EMPTY owner is a real scope here, not "everything": on a no-login
        install every row is owned by `""`, and treating that as unscoped would
        make the filter a no-op exactly where it is the only filter there is.
        `ANY_OWNER` (which is `None`) is how a sweep asks for every owner.

        The default is `""` and not `ANY_OWNER` on purpose. Both defaults have
        a failure mode and they are not symmetric: a route that forgets to pass
        an owner gets the blank owner's rows, which on a multi-user install is
        an empty list, and on a single-user install is the right answer. The
        other way round, the same forgetfulness would return every owner's
        state to whoever asked.
        """
        prefix = f"{alias}." if alias else ""
        clauses: List[str] = []
        params: List[Any] = []
        if owner is not None:
            clauses.append(f"{prefix}owner = ?")
            params.append(str(owner or ""))
        if namespace:
            clauses.append(f"{prefix}namespace = ?")
            params.append(str(namespace))
        if project_id:
            clauses.append(f"{prefix}project_id = ?")
            params.append(str(project_id))
        return (" AND ".join(clauses) if clauses else "1=1"), params

    # -- entities ---------------------------------------------------------

    def upsert_entity(self, entity: StateEntity) -> StateEntity:
        """Record that this entity exists. Idempotent by id.

        `created_at` is preserved on a second call: an entity re-discovered by
        a sweep is the same entity, and moving its birthday would make "new
        since yesterday" a lie.
        """
        data = entity.to_dict()
        try:
            with self._db() as conn:
                existing = conn.execute(
                    "SELECT created_at FROM state_entities WHERE id = ?",
                    [entity.id]).fetchone()
                created = (existing["created_at"] if existing
                           else (entity.created_at or now_iso()))
                conn.execute(
                    "INSERT INTO state_entities (id, kind, owner, namespace, "
                    "project_id, display_name, labels, source_refs, schema, "
                    "created_at, updated_at, retired_at, sensitivity, schema_version) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET kind=excluded.kind, "
                    "owner=excluded.owner, namespace=excluded.namespace, "
                    "project_id=excluded.project_id, "
                    "display_name=excluded.display_name, labels=excluded.labels, "
                    "source_refs=excluded.source_refs, schema=excluded.schema, "
                    "updated_at=excluded.updated_at, retired_at=excluded.retired_at, "
                    "sensitivity=excluded.sensitivity",
                    [entity.id, entity.kind, entity.owner, entity.namespace,
                     entity.project_id, entity.display_name,
                     _dumps(data["labels"]), _dumps(data["source_refs"]),
                     entity.schema, created, entity.updated_at or now_iso(),
                     entity.retired_at, entity.sensitivity, entity.schema_version])
        except sqlite3.Error as exc:
            raise StateStoreError("entity", f"could not store {entity.id}",
                                  got=str(exc)) from exc
        return entity

    def get_entity(self, entity_id: str) -> Optional[StateEntity]:
        row = self._one("SELECT * FROM state_entities WHERE id = ?", [str(entity_id)])
        return self._entity(row) if row else None

    def list_entities(self, *, owner: Any = "", namespace: str = "",
                      project_id: str = "", kind: str = "",
                      include_retired: bool = False,
                      limit: int = 200) -> List[StateEntity]:
        where, params = self._scope(owner, namespace, project_id)
        if kind:
            where += " AND kind = ?"
            params.append(str(kind))
        if not include_retired:
            where += " AND retired_at = ''"
        rows = self._all(
            f"SELECT * FROM state_entities WHERE {where} ORDER BY kind, id LIMIT ?",
            params + [max(1, min(int(limit or 200), 2000))])
        return _kept(self._entity(r) for r in rows)

    def retire_entity(self, entity_id: str, *, at: str = "") -> bool:
        """Mark an entity gone without deleting what we knew about it."""
        try:
            with self._db() as conn:
                cursor = conn.execute(
                    "UPDATE state_entities SET retired_at = ?, updated_at = ? "
                    "WHERE id = ? AND retired_at = ''",
                    [at or now_iso(), now_iso(), str(entity_id)])
                return cursor.rowcount > 0
        except sqlite3.Error as exc:
            raise StateStoreError("entity", f"could not retire {entity_id}",
                                  got=str(exc)) from exc

    # -- observations -----------------------------------------------------

    def append_observation(self, observation: StateObservation) -> Tuple[bool, str]:
        """Store one observation. `(stored, id)`.

        `(False, existing_id)` means the identity index already had it -- a
        replay, a retry, a reconnecting subscription. That is a NORMAL outcome
        and not an error: section 8.1 asks for dedupe, and the caller uses the
        boolean to decide whether the reducer needs to run at all.
        """
        data = observation.to_dict()
        identity = observation.identity()
        try:
            with self._db() as conn:
                existing = conn.execute(
                    "SELECT id FROM state_observations WHERE identity = ?",
                    [identity]).fetchone()
                if existing:
                    return False, str(existing["id"])
                conn.execute(
                    "INSERT INTO state_observations (id, identity, entity_id, "
                    "owner, namespace, project_id, schema, source, epistemic, "
                    "sequence, observed_at, received_at, valid_for_seconds, "
                    "partial, state, evidence_refs, source_revision, sensitivity, "
                    "schema_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [observation.id, identity, observation.entity_id,
                     observation.owner, observation.namespace,
                     observation.project_id, observation.schema,
                     observation.source, observation.epistemic,
                     observation.sequence, observation.observed_at,
                     observation.received_at, observation.valid_for_seconds,
                     1 if observation.partial else 0, _dumps(data["state"]),
                     _dumps(data["evidence_refs"]), observation.source_revision,
                     observation.sensitivity, observation.schema_version])
        except sqlite3.IntegrityError:
            # Lost a race on the identity index. The other writer stored the
            # same fact, so this is the same normal outcome as above.
            row = self._one("SELECT id FROM state_observations WHERE identity = ?",
                            [identity])
            return False, str(row["id"]) if row else observation.id
        except sqlite3.Error as exc:
            raise StateStoreError("observation",
                                  f"could not store an observation of "
                                  f"{observation.entity_id}", got=str(exc)) from exc
        return True, observation.id

    def observations(self, entity_id: str, *, limit: int = 50,
                     source: str = "") -> List[StateObservation]:
        """The log for one entity, newest first. Reads never raise."""
        where = "entity_id = ?"
        params: List[Any] = [str(entity_id)]
        if source:
            where += " AND source = ?"
            params.append(str(source))
        rows = self._all(
            f"SELECT * FROM state_observations WHERE {where} "
            "ORDER BY received_at DESC, rowid DESC LIMIT ?",
            params + [max(1, min(int(limit or 50), 500))])
        return _kept(self._observation(r) for r in rows)

    def observation_cursor(self, entity_id: str) -> int:
        row = self._one("SELECT COALESCE(MAX(rowid), 0) AS cursor FROM state_observations WHERE entity_id = ?",
                        [str(entity_id)])
        return int(row["cursor"]) if row else 0

    def latest_field_observations(self, entity_id: str, field: str) -> List[StateObservation]:
        """Newest measurement per source, not newest arrival of an old event.

        Complete samples are retained even when they omit the field: omission
        cannot be used as evidence that the sources agree. A 33rd source is a
        sentinel; reconciliation declines oversized source sets conservatively.
        """
        rows = self._all(
            "WITH ranked AS (SELECT *, ROW_NUMBER() OVER (PARTITION BY source "
            "ORDER BY julianday(observed_at) DESC, sequence DESC, rowid DESC) AS rank "
            "FROM state_observations WHERE entity_id = ? "
            "AND (json_type(state, ?) IS NOT NULL OR partial = 0)) "
            "SELECT * FROM ranked WHERE rank = 1 LIMIT 33",
            [str(entity_id), '$.' + json.dumps(str(field))])
        return _kept(self._observation(r) for r in rows)

    # -- materialised state -----------------------------------------------

    def put_state(self, state: MaterializedState, *, changed: bool = True) -> int:
        """Write the reduced state. Returns the change cursor it was given.

        `changed=False` writes the row without advancing the cursor: a
        reduction that only refreshed freshness has new metadata to store and
        nothing a consumer following changes needs to wake up for.
        """
        data = state.to_dict()
        try:
            with self._db() as conn:
                from src.state_mirror import replay
                row = conn.execute("SELECT * FROM state_materialized WHERE entity_id=?", [state.entity_id]).fetchone()
                previous = self._state(row) if row else None
                replay.record(conn, previous.to_dict() if previous else None, data)
                if changed:
                    seq = self._next_seq(conn)
                else:
                    row = conn.execute(
                        "SELECT changed_seq FROM state_materialized WHERE entity_id = ?",
                        [state.entity_id]).fetchone()
                    seq = int(row["changed_seq"]) if row else 0
                conn.execute(
                    "INSERT INTO state_materialized (entity_id, owner, namespace, "
                    "project_id, schema, revision, fields, conflicts, updated_at, "
                    "changed_seq, schema_version) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(entity_id) DO UPDATE SET owner=excluded.owner, "
                    "namespace=excluded.namespace, project_id=excluded.project_id, "
                    "schema=excluded.schema, revision=excluded.revision, "
                    "fields=excluded.fields, conflicts=excluded.conflicts, "
                    "updated_at=excluded.updated_at, changed_seq=excluded.changed_seq",
                    [state.entity_id, state.owner, state.namespace,
                     state.project_id, state.schema, state.revision,
                     _dumps(data["fields"]), _dumps(data["conflicts"]),
                     state.updated_at, seq, state.schema_version])
                return seq
        except sqlite3.Error as exc:
            raise StateStoreError("state", f"could not store {state.entity_id}",
                                  got=str(exc)) from exc

    def get_state(self, entity_id: str) -> Optional[MaterializedState]:
        row = self._one("SELECT * FROM state_materialized WHERE entity_id = ?",
                        [str(entity_id)])
        return self._state(row) if row else None

    def rebuild_state(self, entity_id: str, *, owner: str, apply: bool = False,
                      expected_sha256: str = "") -> Dict[str, Any]:
        """Verify the committed transition journal; optionally repair its read model.

        A caller must first inspect a receipt, then apply that exact head. The
        journal, scope check and repair share a write lock, so a concurrent probe
        cannot be overwritten with a stale reconstruction.
        """
        from src.state_mirror import replay
        if not owner:
            raise NotFound("entity", entity_id)
        with self._db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            entity = conn.execute("SELECT owner FROM state_entities WHERE id=?", [entity_id]).fetchone()
            if entity is None or entity["owner"] != owner:
                raise NotFound("entity", entity_id)
            data, receipt = replay.verify(conn, entity_id)
            state = MaterializedState.parse(data)
            if state.entity_id != entity_id or state.owner != owner:
                raise ValueError("journal scope does not match its entity")
            row = conn.execute("SELECT * FROM state_materialized WHERE entity_id=?", [entity_id]).fetchone()
            live = self._state(row) if row else None
            matches = bool(live and replay.digest(live.to_dict()) == receipt["sha256"])
            if apply and expected_sha256 != receipt["sha256"]:
                raise ValueError("journal changed; verify the current receipt before applying")
            repaired = bool(apply and not matches)
            if repaired:
                seq = self._next_seq(conn)
                conn.execute(
                    "INSERT INTO state_materialized (entity_id, owner, namespace, project_id, schema, "
                    "revision, fields, conflicts, updated_at, changed_seq, schema_version) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(entity_id) DO UPDATE SET "
                    "owner=excluded.owner, namespace=excluded.namespace, project_id=excluded.project_id, "
                    "schema=excluded.schema, revision=excluded.revision, fields=excluded.fields, "
                    "conflicts=excluded.conflicts, updated_at=excluded.updated_at, "
                    "changed_seq=excluded.changed_seq, schema_version=excluded.schema_version",
                    [entity_id, owner, state.namespace, state.project_id, state.schema, state.revision,
                     _dumps(data["fields"]), _dumps(data["conflicts"]), state.updated_at, seq, state.schema_version])
            return {"ok": True, "entity_id": entity_id, "matches": matches,
                    "repaired": repaired, "receipt": receipt, "state": data}

    def list_states(self, *, owner: Any = "", namespace: str = "",
                    project_id: str = "", kind: str = "",
                    limit: int = 200) -> List[MaterializedState]:
        where, params = self._scope(owner, namespace, project_id, alias="m")
        sql = ("SELECT m.* FROM state_materialized m "
               "JOIN state_entities e ON e.id = m.entity_id "
               f"WHERE {where} AND e.retired_at = ''")
        if kind:
            sql += " AND e.kind = ?"
            params.append(str(kind))
        sql += " ORDER BY m.updated_at DESC LIMIT ?"
        rows = self._all(sql, params + [max(1, min(int(limit or 200), 2000))])
        return _kept(self._state(r) for r in rows)

    def changed_since(self, cursor: int, *, owner: Any = "", namespace: str = "",
                      project_id: str = "", limit: int = 200
                      ) -> Tuple[List[MaterializedState], int]:
        """States that changed after `cursor`, and the new cursor.

        The returned cursor is the highest `changed_seq` in the page, so a
        caller that consumes a page and asks again gets strictly what followed.
        On an empty page the cursor comes back unchanged rather than reset --
        resetting is how a poller silently starts replaying history.
        """
        where, params = self._scope(owner, namespace, project_id)
        rows = self._all(
            f"SELECT * FROM state_materialized WHERE {where} AND changed_seq > ? "
            "ORDER BY changed_seq ASC LIMIT ?",
            params + [max(0, int(cursor or 0)),
                      max(1, min(int(limit or 200), 1000))])
        states = _kept(self._state(r) for r in rows)
        highest = max((int(r["changed_seq"]) for r in rows), default=int(cursor or 0))
        return states, highest

    def head_cursor(self) -> int:
        row = self._one("SELECT value FROM state_sequence WHERE name = 'changed'", [])
        return int(row["value"]) if row else 0

    # -- relations --------------------------------------------------------

    def upsert_relation(self, relation: StateRelation) -> StateRelation:
        try:
            with self._db() as conn:
                conn.execute(
                    "UPDATE state_relations SET retired_at = ? "
                    "WHERE rel_key = ? AND retired_at = '' AND id <> ?",
                    [now_iso(), relation.key(), relation.id])
                conn.execute(
                    "INSERT INTO state_relations (id, rel_key, from_id, to_id, "
                    "kind, owner, namespace, origin, source, observed_at, "
                    "created_at, retired_at, schema_version) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET origin=excluded.origin, "
                    "source=excluded.source, observed_at=excluded.observed_at, "
                    "retired_at=excluded.retired_at",
                    [relation.id, relation.key(), relation.from_id, relation.to_id,
                     relation.kind, relation.owner, relation.namespace,
                     relation.origin, relation.source, relation.observed_at,
                     relation.created_at or now_iso(), relation.retired_at,
                     relation.schema_version])
        except sqlite3.Error as exc:
            raise StateStoreError("relation", f"could not store {relation.key()}",
                                  got=str(exc)) from exc
        return relation

    def relations(self, entity_id: str, *, kinds: Sequence[str] = (),
                  direction: str = "both", limit: int = 200) -> List[StateRelation]:
        """Live edges touching this entity, from either end of the single row."""
        target = str(entity_id)
        if direction == "out":
            where, params = "from_id = ?", [target]
        elif direction == "in":
            where, params = "to_id = ?", [target]
        else:
            where, params = "(from_id = ? OR to_id = ?)", [target, target]
        where += " AND retired_at = ''"
        wanted = [str(k) for k in kinds if str(k)]
        if wanted:
            where += f" AND kind IN ({','.join('?' * len(wanted))})"
            params.extend(wanted)
        rows = self._all(
            f"SELECT * FROM state_relations WHERE {where} ORDER BY kind, to_id LIMIT ?",
            params + [max(1, min(int(limit or 200), 1000))])
        return _kept(self._relation(r) for r in rows)

    # -- conflicts --------------------------------------------------------

    def open_conflict(self, conflict: StateConflict) -> Tuple[bool, str]:
        """Record a disagreement. `(opened, id)`.

        `(False, existing_id)` when one is already live for this (entity,
        field): the same two sources disagreeing again is the same conflict
        continuing, not a new one, and a route that showed a hundred rows for
        one argument would be unreadable.
        """
        data = conflict.to_dict()
        try:
            with self._db() as conn:
                existing = conn.execute(
                    "SELECT id FROM state_conflicts WHERE conflict_key = ? "
                    "AND status = 'reconciling'", [conflict.key()]).fetchone()
                if existing:
                    return False, str(existing["id"])
                conn.execute(
                    "INSERT INTO state_conflicts (id, conflict_key, entity_id, "
                    "field, owner, namespace, status, claims, next_check, "
                    "detected_at, resolved_at, resolution, schema_version) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [conflict.id, conflict.key(), conflict.entity_id,
                     conflict.field, conflict.owner, conflict.namespace,
                     conflict.status, _dumps(data["claims"]), conflict.next_check,
                     conflict.detected_at, conflict.resolved_at,
                     conflict.resolution, conflict.schema_version])
        except sqlite3.IntegrityError:
            row = self._one("SELECT id FROM state_conflicts WHERE conflict_key = ? "
                            "AND status = 'reconciling'", [conflict.key()])
            return False, str(row["id"]) if row else conflict.id
        except sqlite3.Error as exc:
            raise StateStoreError("conflict", f"could not open {conflict.key()}",
                                  got=str(exc)) from exc
        return True, conflict.id

    def settle_conflict(self, conflict_id: str, *, status: str,
                        resolution: str = "", observation_cursor: Optional[int] = None) -> bool:
        if status not in ("resolved", "superseded", "abandoned"):
            raise ValueError("a conflict can only settle to a terminal status")
        try:
            with self._db() as conn:
                guard = ""
                params = [str(status), str(resolution or ""), now_iso(), str(conflict_id)]
                if observation_cursor is not None:
                    guard = (" AND (SELECT COALESCE(MAX(rowid), 0) FROM state_observations "
                             "WHERE entity_id = state_conflicts.entity_id) = ?")
                    params.append(int(observation_cursor))
                cursor = conn.execute(
                    "UPDATE state_conflicts SET status = ?, resolution = ?, "
                    "resolved_at = ? WHERE id = ? AND status = 'reconciling'" + guard, params)
                return cursor.rowcount > 0
        except sqlite3.Error as exc:
            raise StateStoreError("conflict", f"could not settle {conflict_id}",
                                  got=str(exc)) from exc

    def conflicts(self, *, owner: Any = "", namespace: str = "",
                  entity_id: str = "", open_only: bool = True,
                  limit: int = 100) -> List[StateConflict]:
        where, params = self._scope(owner, namespace)
        if entity_id:
            where += " AND entity_id = ?"
            params.append(str(entity_id))
        if open_only:
            where += " AND status = 'reconciling'"
        rows = self._all(
            f"SELECT * FROM state_conflicts WHERE {where} "
            "ORDER BY detected_at DESC LIMIT ?",
            params + [max(1, min(int(limit or 100), 500))])
        return _kept(self._conflict(r) for r in rows)

    # -- source cursors and health ----------------------------------------

    def note_source(self, source: str, *, scope: str = "", cursor: str = "",
                    health: str = "ok", detail: str = "",
                    observations: int = 0, failed: bool = False) -> None:
        """Record what a source did. Never raises: diagnostics are not the job.

        A source that cannot even record its own health is a source whose
        failure would otherwise be invisible, so this swallows its errors and
        logs. The alternative -- an adapter dying because bookkeeping failed --
        would turn a degraded source into a missing one.
        """
        try:
            with self._db() as conn:
                stamp = now_iso()
                conn.execute(
                    "INSERT INTO state_source_cursors (source, scope, cursor, "
                    "health, detail, observations, failures, last_ok_at, "
                    "last_error_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(source, scope) DO UPDATE SET "
                    "cursor=CASE WHEN excluded.cursor <> '' THEN excluded.cursor "
                    "            ELSE state_source_cursors.cursor END, "
                    "health=excluded.health, detail=excluded.detail, "
                    "observations=state_source_cursors.observations + excluded.observations, "
                    "failures=state_source_cursors.failures + excluded.failures, "
                    "last_ok_at=CASE WHEN excluded.last_ok_at <> '' "
                    "                THEN excluded.last_ok_at "
                    "                ELSE state_source_cursors.last_ok_at END, "
                    "last_error_at=CASE WHEN excluded.last_error_at <> '' "
                    "                   THEN excluded.last_error_at "
                    "                   ELSE state_source_cursors.last_error_at END, "
                    "updated_at=excluded.updated_at",
                    [str(source), str(scope or ""), str(cursor or ""),
                     str(health or "unknown"), str(detail or "")[:512],
                     max(0, int(observations or 0)), 1 if failed else 0,
                     "" if failed else stamp, stamp if failed else "", stamp])
        except sqlite3.Error as exc:
            logger.warning("state mirror: could not record source %s: %s", source, exc)

    def source_cursor(self, source: str, *, scope: str = "") -> str:
        row = self._one("SELECT cursor FROM state_source_cursors "
                        "WHERE source = ? AND scope = ?",
                        [str(source), str(scope or "")])
        return str(row["cursor"]) if row else ""

    def sources(self) -> List[Dict[str, Any]]:
        rows = self._all("SELECT * FROM state_source_cursors ORDER BY source, scope", [])
        return [dict(r) for r in rows]

    # -- maintenance ------------------------------------------------------

    def compact(self, *, keep_per_entity: int = 100,
                keep_total: int = 200_000) -> Dict[str, int]:
        """Drop old observations. Section 16's retention, made cheap.

        Two limits because they fail differently: one busy entity can bury
        everything else (the per-entity cap), and a thousand quiet ones can
        still fill a disk (the total). The materialised state is never touched
        -- it is the answer; the observations are only the working.
        """
        removed = 0
        try:
            with self._db() as conn:
                cursor = conn.execute(
                    "DELETE FROM state_observations WHERE id IN ("
                    "  SELECT id FROM ("
                    "    SELECT id, ROW_NUMBER() OVER ("
                    "      PARTITION BY entity_id ORDER BY received_at DESC, rowid DESC"
                    "    ) AS rank FROM state_observations"
                    "  ) WHERE rank > ?)", [max(1, int(keep_per_entity or 100))])
                removed += max(0, cursor.rowcount)
                cursor = conn.execute(
                    "DELETE FROM state_observations WHERE id IN ("
                    "  SELECT id FROM state_observations "
                    "  ORDER BY received_at DESC, rowid DESC LIMIT -1 OFFSET ?)",
                    [max(1000, int(keep_total or 200_000))])
                removed += max(0, cursor.rowcount)
        except sqlite3.Error as exc:
            logger.warning("state mirror: compaction failed: %s", exc)
        return {"observations_removed": removed}

    def recover(self) -> Dict[str, Any]:
        """What a restart has to say about what it found (section 16).

        This store has NO in-flight state to reconcile -- an observation is
        either written or it is not -- so recovery is a report rather than a
        repair: how much is held, how old the newest look is, and which sources
        were last seen failing. The ageing itself happens on read, in
        `reducers.rerate`, which is the only correct place for it: a restart
        does not know how long it was down, and a clock does.
        """
        report: Dict[str, Any] = {"at": now_iso(), "counts": {}, "sources": []}
        for table in ("state_entities", "state_observations", "state_materialized",
                      "state_relations", "state_conflicts"):
            row = self._one(f"SELECT count(*) AS n FROM {table}", [])
            report["counts"][table] = int(row["n"]) if row else 0
        row = self._one("SELECT max(observed_at) AS newest FROM state_observations", [])
        report["newest_observation"] = str(row["newest"] or "") if row else ""
        report["cursor"] = self.head_cursor()
        report["sources"] = [
            {"source": s["source"], "scope": s["scope"], "health": s["health"],
             "failures": s["failures"], "last_error_at": s["last_error_at"]}
            for s in self.sources()
        ]
        return report

    def stats(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"path": self.path(), "counts": {}}
        for table in ("state_entities", "state_observations", "state_materialized",
                      "state_relations", "state_conflicts", "state_source_cursors"):
            row = self._one(f"SELECT count(*) AS n FROM {table}", [])
            out["counts"][table] = int(row["n"]) if row else -1
        total = 0
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(OSError):
                total += os.path.getsize(f"{self.path()}{suffix}")
        out["bytes"] = total
        out["cursor"] = self.head_cursor()
        return out

    # -- readers ----------------------------------------------------------
    #
    # Both swallow and log. A read that raised on the turn path would let the
    # mirror break the thing it exists to describe.

    def _one(self, sql: str, params: Sequence[Any]) -> Optional[sqlite3.Row]:
        try:
            with self._db() as conn:
                return conn.execute(sql, list(params)).fetchone()
        except Exception:  # noqa: BLE001 - reads never raise
            logger.exception("state mirror: a read failed: %s", sql.split(" WHERE ")[0])
            return None

    def _all(self, sql: str, params: Sequence[Any]) -> List[sqlite3.Row]:
        try:
            with self._db() as conn:
                return list(conn.execute(sql, list(params)).fetchall())
        except Exception:  # noqa: BLE001 - reads never raise
            logger.exception("state mirror: a read failed: %s", sql.split(" WHERE ")[0])
            return []

    # -- row -> contract ---------------------------------------------------
    #
    # A row that will not parse is logged and skipped rather than raised: one
    # bad row must not make a whole list unreadable, and the row is still in the
    # file for whoever wants to look at it.

    @staticmethod
    def _entity(row: sqlite3.Row) -> Optional[StateEntity]:
        try:
            return StateEntity.parse({
                "id": row["id"], "kind": row["kind"], "owner": row["owner"],
                "namespace": row["namespace"], "project_id": row["project_id"],
                "display_name": row["display_name"],
                "labels": _loads(row["labels"], []),
                "source_refs": _loads(row["source_refs"], []),
                "schema": row["schema"], "created_at": row["created_at"],
                "updated_at": row["updated_at"], "retired_at": row["retired_at"],
                "sensitivity": row["sensitivity"],
                "schema_version": row["schema_version"],
            })
        except StateError:
            logger.exception("state mirror: an entity row will not parse: %s",
                             row["id"])
            return None

    @staticmethod
    def _observation(row: sqlite3.Row) -> Optional[StateObservation]:
        try:
            return StateObservation.parse({
                "id": row["id"], "entity_id": row["entity_id"],
                "owner": row["owner"], "namespace": row["namespace"],
                "project_id": row["project_id"], "schema": row["schema"],
                "source": row["source"], "epistemic": row["epistemic"],
                "sequence": row["sequence"], "observed_at": row["observed_at"],
                "received_at": row["received_at"],
                "valid_for_seconds": row["valid_for_seconds"],
                "partial": bool(row["partial"]),
                "state": _loads(row["state"], {}),
                "evidence_refs": _loads(row["evidence_refs"], []),
                "source_revision": row["source_revision"],
                "sensitivity": row["sensitivity"],
                "schema_version": row["schema_version"],
            })
        except StateError:
            logger.exception("state mirror: an observation row will not parse: %s",
                             row["id"])
            return None

    @staticmethod
    def _state(row: sqlite3.Row) -> Optional[MaterializedState]:
        try:
            return MaterializedState.parse({
                "entity_id": row["entity_id"], "owner": row["owner"],
                "namespace": row["namespace"], "project_id": row["project_id"],
                "schema": row["schema"], "revision": row["revision"],
                "fields": _loads(row["fields"], {}),
                "conflicts": _loads(row["conflicts"], []),
                "updated_at": row["updated_at"],
                "schema_version": row["schema_version"],
            })
        except StateError:
            logger.exception("state mirror: a state row will not parse: %s",
                             row["entity_id"])
            return None

    @staticmethod
    def _relation(row: sqlite3.Row) -> Optional[StateRelation]:
        try:
            return StateRelation.parse({
                "id": row["id"], "from_id": row["from_id"], "to_id": row["to_id"],
                "kind": row["kind"], "owner": row["owner"],
                "namespace": row["namespace"], "origin": row["origin"],
                "source": row["source"], "observed_at": row["observed_at"],
                "created_at": row["created_at"], "retired_at": row["retired_at"],
                "schema_version": row["schema_version"],
            })
        except StateError:
            logger.exception("state mirror: a relation row will not parse: %s",
                             row["id"])
            return None

    @staticmethod
    def _conflict(row: sqlite3.Row) -> Optional[StateConflict]:
        try:
            return StateConflict.parse({
                "id": row["id"], "entity_id": row["entity_id"],
                "field": row["field"], "owner": row["owner"],
                "namespace": row["namespace"], "status": row["status"],
                "claims": _loads(row["claims"], []),
                "next_check": row["next_check"],
                "detected_at": row["detected_at"],
                "resolved_at": row["resolved_at"],
                "resolution": row["resolution"],
                "schema_version": row["schema_version"],
            })
        except StateError:
            logger.exception("state mirror: a conflict row will not parse: %s",
                             row["id"])
            return None
