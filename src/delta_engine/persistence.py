"""Where a delta lives after the comparison is over: one SQLite file, WAL, ours.

Its own database for a reason none of the other three share, and
`src/constants.py::DELTA_ENGINE_DB` already writes it down: what this store
holds is an INTERPRETATION with a long life. `context_engine.db` is a derived
index, `state_mirror.db` is a projection that can be rebuilt from the sources
that own the facts -- both are caches with good manners. A delta is not. It is
the record of what a comparison concluded at a moment, against two named
hashes, with named extractor versions, and a council decision or a proof can
point at one months later and must still read the same. Mixing that into a file
that gets rebuilt would make history depend on a cache.

Nothing here holds a blob. Evidence is a REFERENCE (§18) and lives in the store
that already owns it under its own retention, so this file stays small enough
that quarantining a damaged one costs the interpretations and never the
artifacts they point at.

The asymmetry the rest of the package relies on, same as `state_mirror` and
`council`: **reads never raise** -- they answer `None`, `[]` or `{}` and log --
and **writes raise** `DeltaStoreError`, which a route maps to a refusal. A read
that could throw on the turn path would let the record of a comparison break
the run it was describing.

Three invariants live in SQLite rather than in Python, because a rule enforced
in application code is a rule two processes can race through:

* **one live delta per (owner, fingerprint)** (`idx_delta_cache`, partial on
  `superseded = 0`). This is §22's cache made structural: two live deltas with
  the same source, target, intent and extractor versions are the same question
  answered twice, and the day they disagree the reader believes whichever one
  the query happened to order first. Recomputing REPLACES -- `save_delta`
  supersedes the incumbent inside the same transaction as the insert;
* **one row per (delta, assertion)** and **one per (delta, invariant)**
  (`PRIMARY KEY`), so a re-saved delta cannot leave two projections of the same
  assertion behind;
* **the owner is on every row and in every read**, because a delta names a
  private artifact by path and by value.

`delta_assertions` and `delta_invariant_results` are DENORMALISED PROJECTIONS
of the payload, written in the same transaction as it. They exist so that "give
me the deltas with a blocking regression" is an index scan instead of a load
and a JSON parse of every payload in the table. **They are not the truth**: the
payload is. Anything that reads them and then reports it is reading a
convenience index of a document it did not open, and the day the two disagree
the payload wins and the projection is the bug.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import threading
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.constants import DELTA_ENGINE_DB
from src.contracts.base import now_iso
from src.delta_engine.contracts import (
    CLASSIFICATIONS,
    DeltaError,
    DeltaRequest,
    IntentContract,
    UniversalDelta,
    new_id,
)

logger = logging.getLogger(__name__)

__all__ = [
    "DeltaStoreError",
    "NotFound",
    "ANY_OWNER",
    "DeltaStore",
    "register_schema",
    "registered_schemas",
    "db_path",
    "use_path",
    "store",
    "reset_store",
    "now_iso",
]


class DeltaStoreError(DeltaError):
    """A write this store refused. Reads never raise one.

    A `DeltaError`, so a route that already catches the contract's rejections
    catches a storage refusal too: from the caller's side "this delta is not
    well formed" and "this delta could not be written" are the same answer --
    it is not there and here is the field that says why.
    """


class NotFound(DeltaStoreError):
    def __init__(self, kind: str, row_id: str) -> None:
        super().__init__(f"{kind}.id", "no such row here", got=row_id)


#: What a sweep passes as `owner` when it means every owner. `None` rather than
#: `""`, because `""` is a real owner on a no-login install and the two must
#: never collapse into one bucket. No method here DEFAULTS to it: every read
#: takes `owner` as a required keyword, and a caller that wants every owner has
#: to say so in a word that is impossible to type by accident.
ANY_OWNER: Optional[str] = None

_LOCK = threading.RLock()
_SCHEMAS: Dict[str, Tuple[str, ...]] = {}
_PATH_OVERRIDE: Optional[str] = None
_STORE: Optional["DeltaStore"] = None


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
    return _PATH_OVERRIDE or DELTA_ENGINE_DB


def use_path(path: Optional[str]) -> None:
    """Point the store at another file. Tests and the doctor only.

    Also drops the memoised store, because a process still holding a connection
    to the old file would keep answering from it -- which in a test suite reads
    as "the fixture did nothing" and in the doctor reads as "the repair had no
    effect".
    """
    global _PATH_OVERRIDE, _STORE
    with _LOCK:
        _PATH_OVERRIDE = str(path) if path else None
        previous, _STORE = _STORE, None
    if previous is not None:
        previous.close()


def store() -> "DeltaStore":
    """The process-wide store, built on first use."""
    global _STORE
    with _LOCK:
        if _STORE is None:
            _STORE = DeltaStore()
        return _STORE


def reset_store() -> None:
    """Close and forget the process-wide store, keeping the path override.

    Separate from `use_path(None)` because they answer different needs: this one
    is "drop the connection" (a test that wants a cold open of the SAME file, to
    prove quarantine or to prove a row survived a reopen), and `use_path` is
    "point somewhere else".
    """
    global _STORE
    with _LOCK:
        previous, _STORE = _STORE, None
    if previous is not None:
        previous.close()


_CORE_SCHEMA: Tuple[str, ...] = (
    # -- what was asked, frozen ------------------------------------------
    #
    # `fingerprint` is `IntentContract.fingerprint()` -- what was asked
    # independent of when. It is indexed rather than unique: re-freezing the
    # same request is a NEW contract with its own id and its own `frozen_at`,
    # and collapsing the two would lose which one a delta was computed under.
    """
    CREATE TABLE IF NOT EXISTS delta_intents (
        id           TEXT PRIMARY KEY,
        owner        TEXT NOT NULL DEFAULT '',
        project_id   TEXT NOT NULL DEFAULT '',
        domain       TEXT NOT NULL DEFAULT '',
        fingerprint  TEXT NOT NULL DEFAULT '',
        supersedes   TEXT NOT NULL DEFAULT '',
        frozen_at    TEXT NOT NULL DEFAULT '',
        created_at   TEXT NOT NULL DEFAULT '',
        payload      TEXT NOT NULL DEFAULT '{}'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_delta_intent_fingerprint "
    "ON delta_intents (owner, fingerprint, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_delta_intent_scope "
    "ON delta_intents (owner, domain)",
    # -- what was asked for, as a job ------------------------------------
    """
    CREATE TABLE IF NOT EXISTS delta_requests (
        id           TEXT PRIMARY KEY,
        owner        TEXT NOT NULL DEFAULT '',
        project_id   TEXT NOT NULL DEFAULT '',
        domain       TEXT NOT NULL DEFAULT '',
        source_hash  TEXT NOT NULL DEFAULT '',
        target_hash  TEXT NOT NULL DEFAULT '',
        intent_id    TEXT NOT NULL DEFAULT '',
        created_at   TEXT NOT NULL DEFAULT '',
        payload      TEXT NOT NULL DEFAULT '{}'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_delta_request_recent "
    "ON delta_requests (owner, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_delta_request_revisions "
    "ON delta_requests (owner, source_hash, target_hash)",
    # -- the answer -------------------------------------------------------
    #
    # `supersedes`, `superseded_at` and `superseded_reason` are not in the
    # plan's column list and are here because the genealogy has nowhere else to
    # live: `UniversalDelta` has no `supersedes` field (and `reject_unknown`
    # would refuse one), while `IntentContract` does -- so a superseded delta
    # without these columns is an orphan that cannot say what replaced it or
    # why. `supersede()` takes a reason, and a reason with nowhere to go is a
    # parameter that lies.
    """
    CREATE TABLE IF NOT EXISTS deltas (
        id                 TEXT PRIMARY KEY,
        request_id         TEXT NOT NULL DEFAULT '',
        owner              TEXT NOT NULL DEFAULT '',
        project_id         TEXT NOT NULL DEFAULT '',
        domain             TEXT NOT NULL DEFAULT '',
        source_hash        TEXT NOT NULL DEFAULT '',
        target_hash        TEXT NOT NULL DEFAULT '',
        intent_id          TEXT NOT NULL DEFAULT '',
        intent_fingerprint TEXT NOT NULL DEFAULT '',
        assessment         TEXT NOT NULL DEFAULT '',
        fingerprint        TEXT NOT NULL DEFAULT '',
        run_id             TEXT NOT NULL DEFAULT '',
        correlation_id     TEXT NOT NULL DEFAULT '',
        elapsed_ms         INTEGER NOT NULL DEFAULT 0,
        created_at         TEXT NOT NULL DEFAULT '',
        superseded         INTEGER NOT NULL DEFAULT 0,
        supersedes         TEXT NOT NULL DEFAULT '',
        superseded_at      TEXT NOT NULL DEFAULT '',
        superseded_reason  TEXT NOT NULL DEFAULT '',
        payload            TEXT NOT NULL DEFAULT '{}'
    )
    """,
    # THE §22 cache invariant, and the reason `save_delta` is a transaction and
    # not two statements. Two LIVE deltas with one fingerprint are the same
    # comparison answered twice; superseded rows are excluded from the index so
    # the history of what we used to think survives while the present stays
    # single-valued.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_delta_cache "
    "ON deltas (owner, fingerprint) WHERE superseded = 0",
    "CREATE INDEX IF NOT EXISTS idx_delta_recent ON deltas (owner, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_delta_domain ON deltas (owner, domain)",
    # `invalidate_for_revision` scans on these two: a checkpoint that turned out
    # to be wrong invalidates every delta that looked at it, from either end.
    "CREATE INDEX IF NOT EXISTS idx_delta_source ON deltas (owner, source_hash)",
    "CREATE INDEX IF NOT EXISTS idx_delta_target ON deltas (owner, target_hash)",
    "CREATE INDEX IF NOT EXISTS idx_delta_request_link ON deltas (request_id)",
)

#: The denormalised projections of the payload. Registered as their own schema
#: rather than appended to the core one so that a failure to create them costs
#: the fast queries and not the deltas themselves -- which is the honest
#: priority, because the payload is the truth and these are an index over it.
_PROJECTION_SCHEMA: Tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS delta_assertions (
        delta_id       TEXT NOT NULL,
        assertion_id   TEXT NOT NULL,
        path           TEXT NOT NULL DEFAULT '',
        operation      TEXT NOT NULL DEFAULT '',
        classification TEXT NOT NULL DEFAULT '',
        severity       TEXT NOT NULL DEFAULT '',
        confidence     TEXT NOT NULL DEFAULT '',
        tier           TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (delta_id, assertion_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_delta_assertion_delta "
    "ON delta_assertions (delta_id)",
    # What "show me the deltas with a blocking regression" reads, without
    # loading a payload per row.
    "CREATE INDEX IF NOT EXISTS idx_delta_assertion_material "
    "ON delta_assertions (classification, severity)",
    """
    CREATE TABLE IF NOT EXISTS delta_invariant_results (
        delta_id     TEXT NOT NULL,
        invariant_id TEXT NOT NULL,
        status       TEXT NOT NULL DEFAULT '',
        severity     TEXT NOT NULL DEFAULT '',
        confidence   TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (delta_id, invariant_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_delta_invariant_delta "
    "ON delta_invariant_results (delta_id)",
    "CREATE INDEX IF NOT EXISTS idx_delta_invariant_status "
    "ON delta_invariant_results (status, severity)",
)

#: §23's audit trail. Append-only and never updated: a reclassification that
#: could be edited is a reclassification that can be made to have never
#: happened, and the whole value of the row is that somebody put their name on
#: an interpretation.
_RECLASSIFICATION_SCHEMA: Tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS delta_reclassifications (
        id                  TEXT PRIMARY KEY,
        delta_id            TEXT NOT NULL,
        assertion_id        TEXT NOT NULL DEFAULT '',
        from_classification TEXT NOT NULL DEFAULT '',
        to_classification   TEXT NOT NULL DEFAULT '',
        actor               TEXT NOT NULL DEFAULT '',
        reason              TEXT NOT NULL DEFAULT '',
        at                  TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_delta_reclassification_delta "
    "ON delta_reclassifications (delta_id, at)",
)

register_schema("core", _CORE_SCHEMA)
register_schema("projections", _PROJECTION_SCHEMA)
register_schema("reclassifications", _RECLASSIFICATION_SCHEMA)


def _quarantine(path: str, reason: Any) -> None:
    """Move a damaged file aside so the next open starts clean.

    Kept as a `.corrupt` copy rather than deleted, and the difference from the
    State Mirror matters: there, quarantine costs a rebuild from the sources
    that own the facts, and here it costs the interpretations themselves --
    nothing recomputes a delta that nobody asks for again. So the file is moved,
    never removed, and the log says so in a sentence somebody can act on.

    The alternative is worse in both directions: refusing to start costs the
    whole application because of a file nothing on the turn path needs, and
    opening a corrupt file "successfully" costs every write made afterwards.
    """
    for suffix in ("", "-wal", "-shm"):
        target = f"{path}{suffix}"
        if not os.path.exists(target):
            continue
        try:
            os.replace(target, f"{target}.corrupt")
        except OSError as exc:
            logger.warning("delta engine: could not quarantine %s: %s", target, exc)
    logger.error("delta engine: %s was unreadable (%s); it has been moved aside as "
                 "%s.corrupt and a new one has been created. The deltas it held are "
                 "NOT recomputed automatically -- nothing re-asks a question nobody "
                 "asked again", path, reason, path)


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
                logger.warning("delta engine: schema %s failed (%s)", name, exc)


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
    try:
        # OUTSIDE the suppression, and that is the whole point of the line.
        # sqlite opens a file that is not a database perfectly happily and only
        # complains when it is asked to READ one; a probe inside the `suppress`
        # above would be swallowed, `_open` would never see a `DatabaseError`,
        # and `_quarantine` would be dead code that every test of it passes by
        # accident. The council and the State Mirror both learned this the same
        # way.
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        _apply_schema(conn)
        conn.commit()
    except sqlite3.Error:
        # Close the handle before the exception leaves, because the caller's
        # next move is `os.replace`. On Windows that fails with "the process
        # cannot access the file because it is being used by another process"
        # while this connection is open -- so a leaked handle here turns
        # quarantine into a logged warning, leaves the corrupt file exactly
        # where it was, and makes every subsequent open fail the same way
        # forever. The failure is silent on POSIX, which is how a bug like this
        # survives in a file everybody has read.
        with contextlib.suppress(sqlite3.Error):
            conn.close()
        raise
    return conn


def _dumps(value: Any) -> str:
    """JSON for a payload column. `default=str` and sorted keys.

    Sorted because two writes of the same delta must produce the same bytes --
    a payload that differs only in key order makes every `diff` of a database
    dump unreadable. `default=str` because a payload carries whatever an
    adapter put in `extractor_versions` and `threshold`, and the failure mode of
    raising here is a delta that is lost rather than a delta that is ugly.
    """
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError) as exc:
        logger.warning("delta engine: a value would not serialise (%s); stored as "
                       "null so the row survives", exc)
        return "null"


def _loads(raw: Any, default: Any = None) -> Any:
    if raw is None or raw == "":
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _kept(rows: Any) -> List[Any]:
    """Drop the rows that would not parse.

    A single unparseable row -- written by a newer build, or damaged -- must not
    make a whole list unreadable. The row that was skipped is already in the log
    with an exception beside it, and it is still in the file.
    """
    return [row for row in rows if row is not None]


def _limit(value: Any, default: int, ceiling: int) -> int:
    try:
        wanted = int(value)
    except (TypeError, ValueError):
        wanted = default
    return max(1, min(wanted or default, ceiling))


class DeltaStore:
    """Intents, requests, deltas, their projections and their reclassifications.

    One connection per instance, guarded by an `RLock`, in the same shape as
    `state_mirror.persistence.StateStore` and `council.persistence.CouncilStore`.
    Reads answer empty and log; writes raise `DeltaStoreError`.

    Every read takes `owner` as a REQUIRED keyword and a row belonging to
    somebody else is not returned -- not by id, not in a list, and not through
    an error message that would confirm the id exists. `ANY_OWNER` is the only
    way to ask for every owner and no method defaults to it.
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
                raise DeltaStoreError("store", f"{target} cannot be opened",
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

    @staticmethod
    def _owner_clause(owner: Any, *, alias: str = "") -> Tuple[str, List[Any]]:
        """A WHERE fragment for the owner. `ANY_OWNER` (None) means every owner.

        Every other value is matched literally, `""` included -- it is a value
        and never a wildcard. The contracts require a non-blank owner, so a
        blank one should never reach a row here; treating it as "everything"
        anyway would turn the one bug that can produce it into a leak instead of
        an empty list.

        There is no default. `state_mirror` defaults to `""` because a sweep
        there runs constantly and unowned rows are its common case; a delta is
        always computed for somebody who asked, so a caller that forgot the
        owner is a bug, and the signature makes it a `TypeError` at the call
        site instead of another owner's rows in the answer.
        """
        prefix = f"{alias}." if alias else ""
        if owner is ANY_OWNER:
            return "1=1", []
        return f"{prefix}owner = ?", [str(owner or "")]

    # -- intents ----------------------------------------------------------

    def save_intent(self, intent: IntentContract) -> str:
        """Store a frozen contract. Returns its id. Idempotent, never an update.

        Re-saving the SAME contract (same id, same fingerprint) is a no-op and
        answers the id, because a retried request must not be an error. Saving a
        DIFFERENT contract under an id that already exists is refused: rule 3 of
        `contracts.py` says an intent is frozen before the result is seen, and an
        `UPDATE` here is exactly the edit-after-the-fact that makes every
        evaluation score full marks. A change of mind is a new contract whose
        `supersedes` names this one.
        """
        fingerprint = intent.fingerprint()
        try:
            with self._db() as conn:
                existing = conn.execute(
                    "SELECT owner, fingerprint FROM delta_intents WHERE id = ?",
                    [intent.id]).fetchone()
                if existing is not None:
                    if str(existing["fingerprint"]) != fingerprint:
                        raise DeltaStoreError(
                            "intent.id",
                            "already names a frozen contract that asked for "
                            "something else; a frozen intent is never rewritten, "
                            "a revision is a new contract with `supersedes` set",
                            got=intent.id)
                    if str(existing["owner"]) != intent.owner:
                        raise DeltaStoreError(
                            "intent.owner",
                            f"differs from the owner already stored under this id "
                            f"({existing['owner']!r})", got=intent.owner)
                    return intent.id
                conn.execute(
                    "INSERT INTO delta_intents (id, owner, project_id, domain, "
                    "fingerprint, supersedes, frozen_at, created_at, payload) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    [intent.id, intent.owner, intent.project_id, intent.domain,
                     fingerprint, intent.supersedes, intent.frozen_at,
                     intent.created_at or now_iso(), _dumps(intent.to_dict())])
        except sqlite3.Error as exc:
            raise DeltaStoreError("intent", f"could not store {intent.id}",
                                  got=str(exc)) from exc
        return intent.id

    def get_intent(self, intent_id: str, *, owner: str) -> Optional[IntentContract]:
        clause, params = self._owner_clause(owner)
        row = self._one(f"SELECT payload FROM delta_intents WHERE id = ? AND {clause}",
                        [str(intent_id)] + params)
        return self._intent(row) if row else None

    def find_intent(self, fingerprint: str, *, owner: str) -> Optional[IntentContract]:
        """The newest contract this owner froze that asks for exactly this.

        Newest rather than first, because a re-frozen identical contract is the
        one a caller means today; the older ones are still readable by id, which
        is what an audit of "what were we asked back then" needs.
        """
        clause, params = self._owner_clause(owner)
        row = self._one(
            f"SELECT payload FROM delta_intents WHERE fingerprint = ? AND {clause} "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            [str(fingerprint)] + params)
        return self._intent(row) if row else None

    # -- requests ---------------------------------------------------------

    def save_request(self, request: DeltaRequest, *, intent_id: str = "") -> str:
        """Store the job. Returns its id.

        `intent_id` is an argument rather than only a field because the service
        compiles the contract AFTER parsing the request: the request that
        arrived carried prose (`intent_text`), and the contract it was frozen
        into is known one step later. Passing it here links the two without
        rewriting the payload, which would make the stored request differ from
        the one the caller actually sent.
        """
        linked = str(intent_id or request.intent_contract_id or "")
        try:
            with self._db() as conn:
                conn.execute(
                    "INSERT INTO delta_requests (id, owner, project_id, domain, "
                    "source_hash, target_hash, intent_id, created_at, payload) "
                    "VALUES (?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET intent_id=excluded.intent_id",
                    [request.id, request.owner, request.project_id, request.domain,
                     request.source.hash, request.target.hash, linked,
                     request.created_at or now_iso(), _dumps(request.to_dict())])
        except sqlite3.Error as exc:
            raise DeltaStoreError("request", f"could not store {request.id}",
                                  got=str(exc)) from exc
        return request.id

    def get_request(self, request_id: str, *, owner: str) -> Optional[DeltaRequest]:
        clause, params = self._owner_clause(owner)
        row = self._one(f"SELECT payload FROM delta_requests WHERE id = ? AND {clause}",
                        [str(request_id)] + params)
        return self._request(row) if row else None

    # -- deltas -----------------------------------------------------------

    def save_delta(self, delta: UniversalDelta) -> str:
        """Store one delta and REPLACE the live one it recomputes.

        The §22 cache rule, and the reason this is one transaction: a delta with
        the same `fingerprint()` -- same source, same target, same intent, same
        extractor versions -- is the same question, and answering it again
        SUPERSEDES the previous answer instead of sitting beside it. Two live
        rows would be two answers to one question, and every consumer would pick
        whichever the index happened to return first.

        Idempotent by id: re-saving the same delta rewrites its row and its
        projections rather than raising, so a retried write is not an error.
        """
        return self._write_delta(delta, supersedes="", reason="recomputed")

    def _write_delta(self, delta: UniversalDelta, *, supersedes: str,
                     reason: str) -> str:
        fingerprint = delta.fingerprint()
        stamp = now_iso()
        # Checked here rather than left to the projections' PRIMARY KEY so that
        # the refusal names the field and the value. `IntegrityError` from
        # inside a transaction that also touches `idx_delta_cache` would arrive
        # as one sqlite message for two very different mistakes.
        self._reject_repeats("delta.assertions", [a.id for a in delta.assertions])
        self._reject_repeats("delta.invariants",
                             [i.invariant_id for i in delta.invariants])
        try:
            with self._db() as conn:
                conn.execute(
                    "UPDATE deltas SET superseded = 1, superseded_at = ?, "
                    "superseded_reason = ? WHERE owner = ? AND fingerprint = ? "
                    "AND superseded = 0 AND id <> ?",
                    [stamp, str(reason or ""), delta.owner, fingerprint, delta.id])
                conn.execute(
                    "INSERT INTO deltas (id, request_id, owner, project_id, domain, "
                    "source_hash, target_hash, intent_id, intent_fingerprint, "
                    "assessment, fingerprint, run_id, correlation_id, elapsed_ms, "
                    "created_at, superseded, supersedes, superseded_at, "
                    "superseded_reason, payload) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,'','',?) "
                    "ON CONFLICT(id) DO UPDATE SET request_id=excluded.request_id, "
                    "project_id=excluded.project_id, domain=excluded.domain, "
                    "source_hash=excluded.source_hash, target_hash=excluded.target_hash, "
                    "intent_id=excluded.intent_id, "
                    "intent_fingerprint=excluded.intent_fingerprint, "
                    "assessment=excluded.assessment, fingerprint=excluded.fingerprint, "
                    "run_id=excluded.run_id, correlation_id=excluded.correlation_id, "
                    "elapsed_ms=excluded.elapsed_ms, created_at=excluded.created_at, "
                    "superseded=0, superseded_at='', superseded_reason='', "
                    "payload=excluded.payload",
                    [delta.id, delta.request_id, delta.owner, delta.project_id,
                     delta.domain, delta.source.hash, delta.target.hash,
                     delta.intent_contract_id, delta.intent_fingerprint,
                     delta.assessment, fingerprint, delta.run_id,
                     delta.correlation_id, int(delta.elapsed_ms),
                     delta.created_at or stamp, str(supersedes or ""),
                     _dumps(delta.to_dict())])
                self._project(conn, delta)
        except sqlite3.IntegrityError as exc:
            # The only unique index a caller can collide with is `idx_delta_cache`,
            # and reaching it means the UPDATE above did not cover the incumbent
            # -- another writer inserted between the two statements.
            raise DeltaStoreError(
                "delta.fingerprint",
                "collides with a live delta for this owner that this write did "
                "not supersede; another writer stored the same comparison first",
                got=fingerprint) from exc
        except sqlite3.Error as exc:
            raise DeltaStoreError("delta", f"could not store {delta.id}",
                                  got=str(exc)) from exc
        return delta.id

    @staticmethod
    def _reject_repeats(field: str, ids: Sequence[str]) -> None:
        """Refuse a delta whose rows would overwrite each other.

        Two assertions sharing an id project onto one row and the second wins,
        which loses a finding silently -- the same failure `IntentContract.parse`
        already refuses for two invariants with one id.
        """
        seen = set()
        for value in ids:
            if value in seen:
                raise DeltaStoreError(
                    f"{field}[].id",
                    "appears twice in this delta; two rows with one id project "
                    "onto one row and the second would erase the first",
                    got=value)
            seen.add(value)

    @staticmethod
    def _project(conn: sqlite3.Connection, delta: UniversalDelta) -> None:
        """Rewrite the denormalised rows for this delta, in the payload's own
        transaction.

        Deleted and reinserted rather than upserted, because an assertion that
        disappeared from a recomputed delta must disappear from the index too;
        an upsert would leave the old row behind and the index would answer with
        a finding the delta no longer makes.

        These rows are a CONVENIENCE. The payload is the delta. Anything that
        reads them and reports the result is reporting a summary of a document it
        did not open.
        """
        conn.execute("DELETE FROM delta_assertions WHERE delta_id = ?", [delta.id])
        conn.executemany(
            "INSERT INTO delta_assertions (delta_id, assertion_id, path, operation, "
            "classification, severity, confidence, tier) VALUES (?,?,?,?,?,?,?,?)",
            [(delta.id, a.id, a.path, a.operation, a.classification, a.severity,
              a.confidence, a.tier) for a in delta.assertions])
        conn.execute("DELETE FROM delta_invariant_results WHERE delta_id = ?",
                     [delta.id])
        conn.executemany(
            "INSERT INTO delta_invariant_results (delta_id, invariant_id, status, "
            "severity, confidence) VALUES (?,?,?,?,?)",
            [(delta.id, i.invariant_id, i.status, i.severity, i.confidence)
             for i in delta.invariants])

    def get_delta(self, delta_id: str, *, owner: str) -> Optional[UniversalDelta]:
        """One delta by id, superseded ones included.

        Superseded rows stay readable on purpose: a council decision or a proof
        may point at the delta that was live when it was taken, and answering
        `None` because a newer interpretation exists would rewrite what that
        decision was based on.
        """
        clause, params = self._owner_clause(owner)
        row = self._one(f"SELECT payload FROM deltas WHERE id = ? AND {clause}",
                        [str(delta_id)] + params)
        return self._delta(row) if row else None

    def find_delta(self, fingerprint: str, *, owner: str) -> Optional[UniversalDelta]:
        """The LIVE delta for this fingerprint, or `None`. The §22 cache lookup.

        Only `superseded = 0`, which is what makes this a cache and not a
        history: a superseded delta is what we used to think, and handing it back
        as a cache hit would answer today's question with yesterday's extractors.
        """
        clause, params = self._owner_clause(owner)
        row = self._one(
            f"SELECT payload FROM deltas WHERE fingerprint = ? AND superseded = 0 "
            f"AND {clause} LIMIT 1", [str(fingerprint)] + params)
        return self._delta(row) if row else None

    def list_deltas(self, *, owner: str, domain: str = "", project_id: str = "",
                    assessment: str = "", limit: int = 50,
                    cursor: str = "") -> List[UniversalDelta]:
        """Live deltas for one owner, newest first, one page at a time.

        LIVE ONLY. A list that mixed superseded rows in would show the same
        comparison two or three times, one row per interpretation, and the reader
        would have to know which was current to make sense of any of it. History
        is reached by id, which is how the thing that points at it addresses it.

        `cursor` is the opaque `created_at|id` of the last row of the previous
        page. Compound rather than an offset because deltas are inserted while a
        caller pages: an `OFFSET` would skip a row every time one arrived.
        """
        clause, params = self._owner_clause(owner)
        where = [clause, "superseded = 0"]
        if domain:
            where.append("domain = ?")
            params.append(str(domain))
        if project_id:
            where.append("project_id = ?")
            params.append(str(project_id))
        if assessment:
            where.append("assessment = ?")
            params.append(str(assessment))
        after_at, _, after_id = str(cursor or "").partition("|")
        if cursor and not after_id:
            # Never raise on a read: start from the top and say so, because a
            # cursor that cannot be read is a caller bug, and a page silently
            # served from the wrong place is the same bug made invisible.
            logger.warning("delta engine: unreadable list cursor %r; the page "
                           "starts from the newest row", cursor)
        elif after_id:
            where.append("(created_at < ? OR (created_at = ? AND id < ?))")
            params.extend([after_at, after_at, after_id])
        rows = self._all(
            f"SELECT payload FROM deltas WHERE {' AND '.join(where)} "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            params + [_limit(limit, 50, 500)])
        return _kept(self._delta(r) for r in rows)

    @staticmethod
    def page_cursor(delta: UniversalDelta) -> str:
        """The `cursor` that resumes `list_deltas` after this delta.

        Built here rather than in the caller so that the encoding stays one
        thing: a route that assembled the string itself would keep working right
        up to the day the ordering changes.
        """
        return f"{delta.created_at}|{delta.id}"

    def supersede(self, delta_id: str, *, owner: str, reason: str = "") -> bool:
        """Retire one live delta. `True` when this call is what retired it.

        `False` for a delta that is already superseded, does not exist, or
        belongs to somebody else -- all three mean the same thing to the caller
        ("there is no live delta of yours by that id to retire") and telling them
        apart would confirm the existence of another owner's row.
        """
        try:
            with self._db() as conn:
                clause, params = self._owner_clause(owner)
                cursor = conn.execute(
                    f"UPDATE deltas SET superseded = 1, superseded_at = ?, "
                    f"superseded_reason = ? WHERE id = ? AND superseded = 0 "
                    f"AND {clause}",
                    [now_iso(), str(reason or ""), str(delta_id)] + params)
                return cursor.rowcount > 0
        except sqlite3.Error as exc:
            raise DeltaStoreError("delta", f"could not supersede {delta_id}",
                                  got=str(exc)) from exc

    def invalidate_for_revision(self, revision_hash: str, *, owner: str) -> int:
        """Retire every live delta that looked at this revision. Returns how many.

        From EITHER end: a checkpoint that turns out to have been mis-recorded
        invalidates the comparisons that read it as a source and the ones that
        read it as a target, and a sweep that only checked one side would leave
        half of them standing as current.
        """
        target = str(revision_hash or "")
        if not target:
            raise DeltaStoreError(
                "revision_hash",
                "is empty; an empty hash matches the rows of every revision "
                "whose end was never recorded, which is not what a caller "
                "invalidating one revision meant", got=revision_hash)
        try:
            with self._db() as conn:
                clause, params = self._owner_clause(owner)
                cursor = conn.execute(
                    f"UPDATE deltas SET superseded = 1, superseded_at = ?, "
                    f"superseded_reason = ? WHERE superseded = 0 AND {clause} "
                    f"AND (source_hash = ? OR target_hash = ?)",
                    [now_iso(), f"revision {target[:12]} invalidated"]
                    + params + [target, target])
                return max(0, cursor.rowcount)
        except sqlite3.Error as exc:
            raise DeltaStoreError("revision_hash",
                                  f"could not invalidate deltas for {target}",
                                  got=str(exc)) from exc

    # -- reclassification: §23's one hard rule ----------------------------

    def reclassify(self, delta_id: str, assertion_id: str, *, owner: str,
                   classification: str, actor: str,
                   reason: str) -> Optional[UniversalDelta]:
        """Change what an observation MEANS. Never what was observed.

        This is §23 written as code. A reclassification produces a NEW revision
        of the interpretation: the assertion keeps `operation`, `before`,
        `after`, `method`, `tier`, `confidence`, `alignment` and `evidence_refs`
        exactly as the extractor left them, and only `classification` changes.
        The severity is not copied and not chosen here either -- the dict is
        handed to `DeltaAssertion.parse` without one, so the contract derives it
        from the new classification, which is the only place that rule lives.

        A human saying "that was intentional" is a statement about MEANING. If
        it could also move `before` and `after`, the record would no longer say
        what the machine saw, and the next reader could not tell an
        interpretation from an observation -- which is the distinction the whole
        module docstring of `contracts.py` is built on.

        The previous delta is marked `superseded` and the new one records it in
        `supersedes`, so the chain of who thought what, when, and why is intact.
        `None` means there is no delta of yours by that id; everything else that
        is wrong raises and names the field.
        """
        wanted = str(classification or "")
        if wanted not in CLASSIFICATIONS:
            raise DeltaStoreError(
                "classification",
                f"is not a classification this vocabulary has; a word nothing "
                f"routes on is a string in a database. Known: "
                f"{list(CLASSIFICATIONS)}", got=classification)
        if not str(actor or "").strip():
            raise DeltaStoreError(
                "actor", "is empty; a reclassification with nobody's name on it "
                "is an anonymous edit to the record of what happened",
                got=actor)
        if not str(reason or "").strip():
            raise DeltaStoreError(
                "reason", "is empty; the reason is the only part of a "
                "reclassification a later reader can weigh", got=reason)

        clause, params = self._owner_clause(owner)
        row = self._one(
            f"SELECT payload, superseded FROM deltas WHERE id = ? AND {clause}",
            [str(delta_id)] + params)
        if row is None:
            return None
        if int(row["superseded"] or 0):
            raise DeltaStoreError(
                "delta.id",
                "is superseded; reclassifying a retired interpretation would "
                "make it live again and silently retire the one that replaced it",
                got=delta_id)
        delta = self._delta(row)
        if delta is None:
            raise DeltaStoreError("delta.payload",
                                  "will not parse, so it cannot be revised",
                                  got=delta_id)

        target = next((a for a in delta.assertions if a.id == str(assertion_id)), None)
        if target is None:
            raise DeltaStoreError(
                "assertion_id", "is not an assertion of this delta", got=assertion_id)
        if target.classification == wanted:
            raise DeltaStoreError(
                "classification",
                f"is what this assertion already says; a revision that changes "
                f"nothing would supersede a live delta and add a row to the "
                f"audit trail for no decision", got=classification)

        revised = dict(target.to_dict())
        revised["classification"] = wanted
        revised.pop("severity", None)  # derived by the contract, from the new word
        assertions = [dict(a.to_dict()) if a.id != target.id else revised
                      for a in delta.assertions]
        payload = delta.to_dict()
        payload["assertions"] = assertions
        payload["id"] = new_id("delta")
        payload["created_at"] = now_iso()
        try:
            successor = UniversalDelta.parse(payload)
        except DeltaError as exc:
            # The commonest way to get here is asking for `preserved` on a row
            # whose operation says it changed: the contract refuses to hold an
            # observation and a conclusion that contradict each other, and that
            # refusal is the feature.
            raise DeltaStoreError(
                "classification",
                f"cannot be applied to this assertion without contradicting what "
                f"was observed ({exc})", got=classification) from exc

        self._write_delta(successor, supersedes=delta.id, reason="reclassified")
        self._note_reclassification(successor.id, target.id,
                                    was=target.classification, now=wanted,
                                    actor=str(actor), reason=str(reason))
        return successor

    def _note_reclassification(self, delta_id: str, assertion_id: str, *, was: str,
                               now: str, actor: str, reason: str) -> None:
        try:
            with self._db() as conn:
                conn.execute(
                    "INSERT INTO delta_reclassifications (id, delta_id, "
                    "assertion_id, from_classification, to_classification, actor, "
                    "reason, at) VALUES (?,?,?,?,?,?,?,?)",
                    [new_id("reclassification"), delta_id, assertion_id, was, now,
                     actor, reason[:1000], now_iso()])
        except sqlite3.Error as exc:
            raise DeltaStoreError("reclassification",
                                  f"could not record the reclassification of "
                                  f"{assertion_id}", got=str(exc)) from exc

    def reclassifications(self, delta_id: str, *,
                          owner: str) -> List[Dict[str, Any]]:
        """The whole trail behind this delta, newest first. Reads never raise.

        Walks `supersedes` backwards and collects the rows of every ancestor,
        because a delta reclassified twice is three rows in `deltas` and the
        question "who has interpreted this comparison, and why" is about the
        chain and not about the last link of it.
        """
        clause, params = self._owner_clause(owner)
        chain: List[str] = []
        seen = set()
        current = str(delta_id)
        while current and current not in seen and len(chain) < 64:
            seen.add(current)
            row = self._one(
                f"SELECT id, supersedes FROM deltas WHERE id = ? AND {clause}",
                [current] + params)
            if row is None:
                break
            chain.append(str(row["id"]))
            current = str(row["supersedes"] or "")
        if not chain:
            return []
        rows = self._all(
            "SELECT * FROM delta_reclassifications WHERE delta_id IN "
            f"({','.join('?' * len(chain))}) ORDER BY at DESC, rowid DESC", chain)
        return [dict(r) for r in rows]

    # -- counting ---------------------------------------------------------

    def counts(self, *, owner: str) -> Dict[str, int]:
        """How much of this owner's record is here. For diagnostics and tests.

        `deltas` counts every revision and `live_deltas` counts the ones that are
        current; the two differ by exactly the number of comparisons that have
        been recomputed or reinterpreted, which is the number somebody looking
        for a runaway recompute loop actually wants.

        The child tables are counted through a join rather than directly,
        because they carry no owner of their own -- they belong to the delta
        that owns them, and counting them unscoped would tell one user how busy
        another one is.
        """
        clause, params = self._owner_clause(owner)
        joined, _ = self._owner_clause(owner, alias="d")
        out: Dict[str, int] = {}
        for key, sql in (
            ("intents", f"SELECT count(*) AS n FROM delta_intents WHERE {clause}"),
            ("requests", f"SELECT count(*) AS n FROM delta_requests WHERE {clause}"),
            ("deltas", f"SELECT count(*) AS n FROM deltas WHERE {clause}"),
            ("live_deltas",
             f"SELECT count(*) AS n FROM deltas WHERE superseded = 0 AND {clause}"),
            ("superseded_deltas",
             f"SELECT count(*) AS n FROM deltas WHERE superseded = 1 AND {clause}"),
            ("assertions",
             "SELECT count(*) AS n FROM delta_assertions a JOIN deltas d "
             f"ON d.id = a.delta_id WHERE {joined}"),
            ("invariant_results",
             "SELECT count(*) AS n FROM delta_invariant_results i JOIN deltas d "
             f"ON d.id = i.delta_id WHERE {joined}"),
            ("reclassifications",
             "SELECT count(*) AS n FROM delta_reclassifications r JOIN deltas d "
             f"ON d.id = r.delta_id WHERE {joined}"),
        ):
            row = self._one(sql, params)
            out[key] = int(row["n"]) if row else -1
        return out

    # -- readers ----------------------------------------------------------
    #
    # Both swallow and log. A read that raised on the turn path would let the
    # record of a comparison break the run it was describing.

    def _one(self, sql: str, params: Sequence[Any]) -> Optional[sqlite3.Row]:
        try:
            with self._db() as conn:
                return conn.execute(sql, list(params)).fetchone()
        except Exception:  # noqa: BLE001 - reads never raise
            logger.exception("delta engine: a read failed: %s",
                             sql.split(" WHERE ")[0])
            return None

    def _all(self, sql: str, params: Sequence[Any]) -> List[sqlite3.Row]:
        try:
            with self._db() as conn:
                return list(conn.execute(sql, list(params)).fetchall())
        except Exception:  # noqa: BLE001 - reads never raise
            logger.exception("delta engine: a read failed: %s",
                             sql.split(" WHERE ")[0])
            return []

    # -- row -> contract ---------------------------------------------------
    #
    # A payload that will not parse is logged and skipped rather than raised: one
    # damaged row must not make a whole page unreadable, and the row is still in
    # the file for whoever wants to look at it.

    @staticmethod
    def _intent(row: sqlite3.Row) -> Optional[IntentContract]:
        try:
            return IntentContract.parse(_loads(row["payload"], {}))
        except DeltaError:
            logger.exception("delta engine: an intent payload will not parse")
            return None

    @staticmethod
    def _request(row: sqlite3.Row) -> Optional[DeltaRequest]:
        try:
            return DeltaRequest.parse(_loads(row["payload"], {}))
        except DeltaError:
            logger.exception("delta engine: a request payload will not parse")
            return None

    @staticmethod
    def _delta(row: sqlite3.Row) -> Optional[UniversalDelta]:
        try:
            return UniversalDelta.parse(_loads(row["payload"], {}))
        except DeltaError:
            logger.exception("delta engine: a delta payload will not parse")
            return None
