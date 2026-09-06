"""Where a run's ACCOUNT lives once the engine has stopped: one SQLite file, ours.

`src/constants.py::COMPLETION_ENGINE_DB` already says why this store owns a file
of its own, and the reason is not the one `context_engine.db` or
`state_mirror.db` have. Those two are caches with good manners: everything in
them is derived and rebuildable, and losing one costs a rebuild. What this store
holds is the ANSWER TO "why did it do more than I asked" -- what was promised
before the run started, what was added beyond it, what was refused and for which
reason, and what had been spent when it stopped. Nothing recomputes that. The
run is over and the frontier it was standing on went with it. So this file
outlives the caches, and it must never be able to hold a write lock that the
turn producing it is waiting on.

Nothing here holds a blob. Evidence is a REFERENCE -- `proof_refs`,
`delta_refs`, `changeset_refs`, `evidence_refs` -- and lives in the store that
already owns it under its own retention, so this file stays small enough that
quarantining a damaged one costs the accounts and never the artifacts they
point at.

The asymmetry the rest of the package relies on, same as `delta_engine`,
`state_mirror` and `council`: **reads never raise** -- they answer `None`, `[]`
or `{}` and log -- and **writes raise** `CompletionStoreError`, which a route
maps to a refusal. A read that could throw on the turn path would let the
account of a run break the run it was accounting for.

THE RULE THAT IS THIS SUBSYSTEM'S OWN: **a shadow decision and a real one are
never mixed in one query.** `shadow = 1` means the engine recorded what it WOULD
have done without doing any of it, which is how a policy change is measured
before anyone trusts it. Counting those measurements beside what actually
happened ruins the measurement in both directions at once -- the shadow run
inflates the real numbers, and the real work makes the shadow look like it
shipped -- and the ruin is silent, because both halves are well-formed
decisions. So `shadow` is a parameter of every list read and every statistic
here, defaulting to `False` (what really happened); `True` asks for the
measurements instead; and `None` asks for both, exists for diagnostics, and is
documented as such at every signature that takes it. No read here mixes them by
accident, because there is no read here that does not ask.

Three invariants live in SQLite rather than in Python, because a rule enforced
in application code is a rule two processes can race through:

* **one row per (decision, candidate)** and **one per (decision, budget line)**
  (`PRIMARY KEY`), so a re-saved decision cannot leave two projections of the
  same candidate behind;
* **the owner is on every row and in every read**, because a decision names
  private paths and quotes a goal somebody wrote;
* **the query indices carry `shadow` in the key**, so the separation above is
  what the index scans on rather than something a WHERE clause is trusted to
  remember.

`completion_candidates` and `completion_spends` are DENORMALISED PROJECTIONS of
the payload, written in the same transaction as it. They exist so that "how many
improvements did we refuse for budget this month" is an index scan instead of a
load and a JSON parse of every payload in the table -- and that question is the
one that decides whether a budget is too small, which is a question nobody asks
if asking it costs a full scan. **They are not the truth**: the payload is.
Anything that reads them and then reports the result is reporting a summary of a
document it did not open, and the day the two disagree the payload wins and the
projection is the bug.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import threading
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.completion_engine.contracts import (
    CompletionContract,
    CompletionDecision,
    CompletionError,
    ScopeEnvelope,
)
from src.constants import COMPLETION_ENGINE_DB
from src.contracts.base import ContractError, now_iso

logger = logging.getLogger(__name__)

__all__ = [
    "CompletionStoreError",
    "ANY_OWNER",
    "CompletionStore",
    "register_schema",
    "registered_schemas",
    "db_path",
    "use_path",
    "store",
    "reset_store",
    "now_iso",
]


class CompletionStoreError(CompletionError):
    """A write this store refused. Reads never raise one.

    A `CompletionError`, so a route that already catches the contract's
    rejections catches a storage refusal too: from the caller's side "this
    decision is not well formed" and "this decision could not be written" are
    the same answer -- it is not there, and here is the field that says why.
    """


#: What a sweep passes as `owner` when it means every owner. `None` rather than
#: `""`, because `""` is a real owner on a no-login install and the two must
#: never collapse into one bucket. No method here DEFAULTS to it: every read
#: takes `owner` as a required keyword, and a caller that wants every owner has
#: to say so in a word that is impossible to type by accident.
ANY_OWNER: Optional[str] = None

_LOCK = threading.RLock()
_SCHEMAS: Dict[str, Tuple[str, ...]] = {}
_PATH_OVERRIDE: Optional[str] = None
_STORE: Optional["CompletionStore"] = None


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
    return _PATH_OVERRIDE or COMPLETION_ENGINE_DB


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


def store() -> "CompletionStore":
    """The process-wide store, built on first use."""
    global _STORE
    with _LOCK:
        if _STORE is None:
            _STORE = CompletionStore()
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
    # -- the boundary the run worked inside -------------------------------
    #
    # `goal` is lifted out of the payload because it is the one field a human
    # reads to recognise a row; everything else about the envelope is
    # structural and is answered from the payload.
    """
    CREATE TABLE IF NOT EXISTS completion_scopes (
        id         TEXT PRIMARY KEY,
        owner      TEXT NOT NULL DEFAULT '',
        project_id TEXT NOT NULL DEFAULT '',
        goal       TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT '',
        payload    TEXT NOT NULL DEFAULT '{}'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_completion_scope_recent "
    "ON completion_scopes (owner, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_completion_scope_project "
    "ON completion_scopes (owner, project_id)",
    # -- what was promised, before the work ------------------------------
    #
    # `fingerprint` is `CompletionContract.fingerprint()` -- what was promised
    # independent of when. Indexed rather than unique: re-writing the same
    # promise for a second run is a NEW contract with its own id and its own
    # `created_at`, and collapsing the two would lose which run was judged
    # under which.
    """
    CREATE TABLE IF NOT EXISTS completion_contracts (
        id                TEXT PRIMARY KEY,
        owner             TEXT NOT NULL DEFAULT '',
        project_id        TEXT NOT NULL DEFAULT '',
        run_id            TEXT NOT NULL DEFAULT '',
        session_id        TEXT NOT NULL DEFAULT '',
        mode              TEXT NOT NULL DEFAULT '',
        policy_version    TEXT NOT NULL DEFAULT '',
        scope_envelope_id TEXT NOT NULL DEFAULT '',
        fingerprint       TEXT NOT NULL DEFAULT '',
        created_at        TEXT NOT NULL DEFAULT '',
        payload           TEXT NOT NULL DEFAULT '{}'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_completion_contract_recent "
    "ON completion_contracts (owner, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_completion_contract_run "
    "ON completion_contracts (owner, run_id)",
    "CREATE INDEX IF NOT EXISTS idx_completion_contract_mode "
    "ON completion_contracts (owner, mode)",
    "CREATE INDEX IF NOT EXISTS idx_completion_contract_fingerprint "
    "ON completion_contracts (owner, fingerprint)",
    "CREATE INDEX IF NOT EXISTS idx_completion_contract_scope "
    "ON completion_contracts (scope_envelope_id)",
    # -- why it stopped where it stopped ---------------------------------
    #
    # `layers` is `completed_layers` joined with a comma. A denormalised
    # convenience like the two projection tables and true for the same reason:
    # the payload is the record, and this column exists so a listing can show
    # how far a run went without opening one.
    """
    CREATE TABLE IF NOT EXISTS completion_decisions (
        id                TEXT PRIMARY KEY,
        contract_id       TEXT NOT NULL DEFAULT '',
        scope_envelope_id TEXT NOT NULL DEFAULT '',
        owner             TEXT NOT NULL DEFAULT '',
        project_id        TEXT NOT NULL DEFAULT '',
        run_id            TEXT NOT NULL DEFAULT '',
        session_id        TEXT NOT NULL DEFAULT '',
        mode              TEXT NOT NULL DEFAULT '',
        stop_reason       TEXT NOT NULL DEFAULT '',
        shadow            INTEGER NOT NULL DEFAULT 0,
        layers            TEXT NOT NULL DEFAULT '',
        created_at        TEXT NOT NULL DEFAULT '',
        payload           TEXT NOT NULL DEFAULT '{}'
    )
    """,
    # `shadow` is the SECOND column of every one of these on purpose, and this
    # is the module docstring's own rule made structural. The queries this
    # store serves are all "for this owner, of this kind, ...", so a key that
    # led with `(owner, created_at)` alone would leave the separation of
    # measured-from-real to a WHERE clause -- correct until the first reader
    # who forgets it, and fast only by accident afterwards. A prefix of these
    # keys still serves an owner-only scan, so nothing is lost by putting it
    # where it cannot be omitted.
    "CREATE INDEX IF NOT EXISTS idx_completion_decision_recent "
    "ON completion_decisions (owner, shadow, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_completion_decision_run "
    "ON completion_decisions (owner, shadow, run_id)",
    "CREATE INDEX IF NOT EXISTS idx_completion_decision_mode "
    "ON completion_decisions (owner, shadow, mode)",
    "CREATE INDEX IF NOT EXISTS idx_completion_decision_stop "
    "ON completion_decisions (owner, shadow, stop_reason)",
    "CREATE INDEX IF NOT EXISTS idx_completion_decision_project "
    "ON completion_decisions (owner, shadow, project_id)",
    "CREATE INDEX IF NOT EXISTS idx_completion_decision_contract "
    "ON completion_decisions (contract_id)",
)

#: The denormalised projections of the payload. Registered as their own schema
#: rather than appended to the core one so that a failure to create them costs
#: the fast queries and not the accounts themselves -- which is the honest
#: priority, because the payload is the truth and these are an index over it.
_PROJECTION_SCHEMA: Tuple[str, ...] = (
    # One row per candidate the decision carries, whichever list it came from:
    # `status` says which, and `rejection_reason` is why for the ones that did
    # not run. This table is what makes "how many improvements were refused for
    # budget this month" an index scan.
    """
    CREATE TABLE IF NOT EXISTS completion_candidates (
        decision_id      TEXT NOT NULL,
        candidate_id     TEXT NOT NULL,
        layer            TEXT NOT NULL DEFAULT '',
        category         TEXT NOT NULL DEFAULT '',
        relation         TEXT NOT NULL DEFAULT '',
        source           TEXT NOT NULL DEFAULT '',
        status           TEXT NOT NULL DEFAULT '',
        rejection_reason TEXT NOT NULL DEFAULT '',
        expected_value   REAL NOT NULL DEFAULT 0,
        estimated_cost   REAL NOT NULL DEFAULT 0,
        risk             REAL NOT NULL DEFAULT 0,
        PRIMARY KEY (decision_id, candidate_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_completion_candidate_decision "
    "ON completion_candidates (decision_id)",
    "CREATE INDEX IF NOT EXISTS idx_completion_candidate_rejection "
    "ON completion_candidates (rejection_reason, layer)",
    "CREATE INDEX IF NOT EXISTS idx_completion_candidate_layer "
    "ON completion_candidates (layer, status)",
    # One row per BUDGET LINE the decision spent on -- the line the caller
    # named, not the pot `FUNDING_LINES` funds it from. The closeout reports
    # the line, so the account stores the line; rolling it into its pot here
    # would answer a question nobody asked and lose the one they did.
    """
    CREATE TABLE IF NOT EXISTS completion_spends (
        decision_id TEXT NOT NULL,
        line        TEXT NOT NULL,
        rounds      INTEGER NOT NULL DEFAULT 0,
        tool_calls  INTEGER NOT NULL DEFAULT 0,
        tokens      INTEGER NOT NULL DEFAULT 0,
        seconds     REAL NOT NULL DEFAULT 0,
        PRIMARY KEY (decision_id, line)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_completion_spend_decision "
    "ON completion_spends (decision_id)",
    "CREATE INDEX IF NOT EXISTS idx_completion_spend_line "
    "ON completion_spends (line)",
)

#: What a PERSON said no to. Its own schema and its own table, and the reason is
#: not tidiness.
#:
#: Every other rejection in this file is a projection: recomputed from the
#: payload each round, discardable, an index over a truth stored elsewhere. This
#: one is the truth. §1.8 says a rejected opportunity must not reappear without
#: new evidence, and the decision row cannot carry that, because the decision is
#: the record of ONE turn and the refusal has to outlive it -- a candidate the
#: engine will rediscover from the same repository tomorrow, and the day after.
#: Stored against `candidate_key` and not `candidate_id`, because the id is
#: minted per decision: keying on it would record a refusal of a row rather than
#: of an improvement, and the same improvement would come back next turn under a
#: new id having lost nothing but the person's answer.
#:
#: Nothing here is destructive. The decision keeps the candidate, its evidence
#: and its score exactly as the engine produced them; what this table adds is
#: the separate fact that someone was shown it and said no. The interesting
#: question a month later is not what ran -- it is what was offered and turned
#: down, and by whom, and why.
_REFUSAL_SCHEMA: Tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS completion_refusals (
        owner        TEXT NOT NULL,
        candidate_key TEXT NOT NULL,
        decision_id  TEXT NOT NULL DEFAULT '',
        candidate_id TEXT NOT NULL DEFAULT '',
        project_id   TEXT NOT NULL DEFAULT '',
        reason       TEXT NOT NULL DEFAULT '',
        actor        TEXT NOT NULL DEFAULT '',
        at           TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (owner, candidate_key)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_completion_refusal_owner "
    "ON completion_refusals (owner, project_id)",
    "CREATE INDEX IF NOT EXISTS idx_completion_refusal_decision "
    "ON completion_refusals (decision_id)",
)

register_schema("core", _CORE_SCHEMA)
register_schema("projections", _PROJECTION_SCHEMA)
register_schema("refusals", _REFUSAL_SCHEMA)


def _quarantine(path: str, reason: Any) -> None:
    """Move a damaged file aside so the next open starts clean.

    Kept as a `.corrupt` copy rather than deleted, and here the stakes are the
    Delta Engine's rather than the State Mirror's: quarantine there costs a
    rebuild from the sources that own the facts, and quarantine here costs the
    accounts themselves -- nothing recomputes why a run that ended last Tuesday
    stopped where it did. So the file is moved, never removed, and the log says
    so in a sentence somebody can act on.

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
            logger.warning("completion engine: could not quarantine %s: %s",
                           target, exc)
    logger.error("completion engine: %s was unreadable (%s); it has been moved "
                 "aside as %s.corrupt and a new one has been created. The "
                 "accounts it held are NOT recomputed -- the runs they describe "
                 "are over", path, reason, path)


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
                logger.warning("completion engine: schema %s failed (%s)",
                               name, exc)


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
        # accident. The council, the State Mirror and the Delta Engine all
        # learned this the same way.
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

    Sorted because two writes of the same decision must produce the same bytes
    -- a payload that differs only in key order makes every `diff` of a database
    dump unreadable. `default=str` because a payload carries whatever an
    estimator put in a candidate's `detail` or a budget's `seconds`, and the
    failure mode of raising here is an account that is lost rather than one that
    is ugly.
    """
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError) as exc:
        logger.warning("completion engine: a value would not serialise (%s); "
                       "stored as null so the row survives", exc)
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


class CompletionStore:
    """Scopes, contracts, decisions and the two projections of a decision.

    One connection per instance, guarded by an `RLock`, in the same shape as
    `delta_engine.persistence.DeltaStore` and `council.persistence.CouncilStore`.
    Reads answer empty and log; writes raise `CompletionStoreError`.

    Every read takes `owner` as a REQUIRED keyword and a row belonging to
    somebody else is not returned -- not by id, not in a list, and not through
    an error message that would confirm the id exists. `ANY_OWNER` is the only
    way to ask for every owner and no method defaults to it.

    Every read that returns MORE THAN ONE row also takes `shadow`, for the
    reason at the top of this file, and defaults it to `False`: a caller that
    says nothing gets what really happened, never a blend of that with what the
    engine merely predicted.
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
                raise CompletionStoreError("store", f"{target} cannot be opened",
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

    # -- the two clauses every read is built out of -----------------------

    @staticmethod
    def _owner_clause(owner: Any, *, alias: str = "") -> Tuple[str, List[Any]]:
        """A WHERE fragment for the owner. `ANY_OWNER` (None) means every owner.

        Every other value is matched literally, `""` included -- it is a value
        and never a wildcard. The contracts allow a blank owner on a no-login
        install, so `""` has to mean that install's one owner; treating it as
        "everything" would turn a single-user default into a leak the moment a
        second owner appears.

        There is no default. A completion decision is always the account of a
        run somebody asked for, so a caller that forgot the owner is a bug, and
        the signature makes it a `TypeError` at the call site instead of another
        owner's rows in the answer.
        """
        prefix = f"{alias}." if alias else ""
        if owner is ANY_OWNER:
            return "1=1", []
        return f"{prefix}owner = ?", [str(owner or "")]

    @staticmethod
    def _shadow_clause(shadow: Optional[bool], *,
                       alias: str = "") -> Tuple[str, List[Any]]:
        """A WHERE fragment for the measured/real split. `None` means both.

        This is the module docstring's own rule, in the one function every
        multi-row read goes through. `False` is what really happened, `True` is
        what the engine recorded that it WOULD have done, and `None` is both --
        which is a diagnostic answer and never a statistic: a rejection rate or
        a spend total computed over both is a number about nothing, because half
        of it describes work that was never done.
        """
        prefix = f"{alias}." if alias else ""
        if shadow is None:
            return "1=1", []
        return f"{prefix}shadow = ?", [1 if shadow else 0]

    # -- scopes -----------------------------------------------------------

    def save_scope(self, envelope: ScopeEnvelope) -> str:
        """Store the boundary a run worked inside. Returns its id.

        An UPSERT, and this is the one place this store deliberately differs
        from `delta_engine.persistence.save_intent`, which refuses a rewrite.
        The difference is `ScopeEnvelope.narrow()`: it returns an envelope
        carrying the SAME id, by design, because §1.12.1's rule is that an
        envelope is narrowed in place and never widened. A store that refused a
        second save under one id would make the only legal mutation of an
        envelope unstorable, and the caller's fallback would be to mint a new id
        -- which is how a decision ends up pointing at an envelope nobody can
        find.

        The rewrite is safe in exactly one direction, and it is the direction
        `narrow()` goes: every path through it intersects, so a row written
        later can only ever permit LESS than the one it replaced. There is no
        `widen()`, and that absence is what makes this upsert honest.
        """
        try:
            with self._db() as conn:
                conn.execute(
                    "INSERT INTO completion_scopes (id, owner, project_id, goal, "
                    "created_at, payload) VALUES (?,?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET owner=excluded.owner, "
                    "project_id=excluded.project_id, goal=excluded.goal, "
                    "created_at=excluded.created_at, payload=excluded.payload",
                    [envelope.id, envelope.owner, envelope.project_id,
                     envelope.goal, envelope.created_at or now_iso(),
                     _dumps(envelope.to_dict())])
        except sqlite3.Error as exc:
            raise CompletionStoreError("scope", f"could not store {envelope.id}",
                                       got=str(exc)) from exc
        return envelope.id

    def get_scope(self, scope_id: str, *, owner: str) -> Optional[ScopeEnvelope]:
        clause, params = self._owner_clause(owner)
        row = self._one(
            f"SELECT payload FROM completion_scopes WHERE id = ? AND {clause}",
            [str(scope_id)] + params)
        return self._scope(row) if row else None

    # -- contracts --------------------------------------------------------

    def save_contract(self, contract: CompletionContract) -> str:
        """Store what the run promised, before it started. Returns its id.

        Idempotent, never an update. Re-saving the SAME contract (same id, same
        fingerprint) is a no-op and answers the id, because a retried request
        must not be an error. Saving a DIFFERENT contract under an id that
        already exists is refused, and rule 5 of `contracts.py` is why: the
        intent is not rewritten to justify an extra. An `UPDATE` here is exactly
        that rewrite -- a `definition_of_done` edited once the work is visible
        is a checklist that always passes, and the whole point of writing it
        first was that it could fail.

        A change of mind is a new contract with its own id, pointing at the same
        run.
        """
        stamped = contract.fingerprint()
        try:
            with self._db() as conn:
                existing = conn.execute(
                    "SELECT owner, fingerprint FROM completion_contracts "
                    "WHERE id = ?", [contract.id]).fetchone()
                if existing is not None:
                    if str(existing["fingerprint"]) != stamped:
                        raise CompletionStoreError(
                            "contract.id",
                            "already names a contract that promised something "
                            "else; what was promised before the run is never "
                            "rewritten afterwards, and a change of mind is a new "
                            "contract",
                            got=contract.id)
                    if str(existing["owner"]) != contract.owner:
                        raise CompletionStoreError(
                            "contract.owner",
                            f"differs from the owner already stored under this id "
                            f"({existing['owner']!r})", got=contract.owner)
                    return contract.id
                conn.execute(
                    "INSERT INTO completion_contracts (id, owner, project_id, "
                    "run_id, session_id, mode, policy_version, scope_envelope_id, "
                    "fingerprint, created_at, payload) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    [contract.id, contract.owner, contract.project_id,
                     contract.run_id, contract.session_id, contract.mode,
                     contract.policy_version, contract.scope_envelope_id, stamped,
                     contract.created_at or now_iso(),
                     _dumps(contract.to_dict())])
        except sqlite3.Error as exc:
            raise CompletionStoreError("contract",
                                       f"could not store {contract.id}",
                                       got=str(exc)) from exc
        return contract.id

    def get_contract(self, contract_id: str, *,
                     owner: str) -> Optional[CompletionContract]:
        clause, params = self._owner_clause(owner)
        row = self._one(
            f"SELECT payload FROM completion_contracts WHERE id = ? AND {clause}",
            [str(contract_id)] + params)
        return self._contract(row) if row else None

    def contract_for_run(self, run_id: str, *,
                         owner: str) -> Optional[CompletionContract]:
        """The newest contract this owner wrote for that run.

        Newest rather than first, because a run whose promise was revised has
        two rows and the later one is the promise it was finally judged under.
        The earlier one stays readable by id, which is what "what did we
        originally say we would do" needs.

        There is no `shadow` here: a contract is what was promised, and a shadow
        run promises the same thing as the real one -- the difference is in what
        was DONE about it, which is the decision's field and not this one.
        """
        clause, params = self._owner_clause(owner)
        row = self._one(
            f"SELECT payload FROM completion_contracts WHERE run_id = ? "
            f"AND {clause} ORDER BY created_at DESC, rowid DESC LIMIT 1",
            [str(run_id)] + params)
        return self._contract(row) if row else None

    # -- decisions --------------------------------------------------------

    def save_decision(self, decision: CompletionDecision) -> str:
        """Store the account of one stop, with its projections. Returns its id.

        Idempotent by id: re-saving rewrites the row and its projections rather
        than raising, so a retried write is not an error.

        The projections are written inside the SAME transaction as the payload,
        and they are DELETED and reinserted rather than upserted. A candidate
        that a re-saved decision no longer carries has to disappear from the
        index too; an upsert would leave the old row behind and
        `rejection_stats` would go on counting a refusal the account no longer
        makes.
        """
        # Checked here rather than left to the projection's PRIMARY KEY so that
        # the refusal names the field and the value. An `IntegrityError` out of
        # a transaction that touches two tables arrives as one sqlite message
        # for several very different mistakes.
        self._reject_repeats(
            "decision.candidates",
            [c.id for c in decision.executed]
            + [c.id for c in decision.rejected]
            + [c.id for c in decision.deferred])
        try:
            with self._db() as conn:
                conn.execute(
                    "INSERT INTO completion_decisions (id, contract_id, "
                    "scope_envelope_id, owner, project_id, run_id, session_id, "
                    "mode, stop_reason, shadow, layers, created_at, payload) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET "
                    "contract_id=excluded.contract_id, "
                    "scope_envelope_id=excluded.scope_envelope_id, "
                    "owner=excluded.owner, project_id=excluded.project_id, "
                    "run_id=excluded.run_id, session_id=excluded.session_id, "
                    "mode=excluded.mode, stop_reason=excluded.stop_reason, "
                    "shadow=excluded.shadow, layers=excluded.layers, "
                    "created_at=excluded.created_at, payload=excluded.payload",
                    [decision.id, decision.contract_id, decision.scope_envelope_id,
                     decision.owner, decision.project_id, decision.run_id,
                     decision.session_id, decision.mode, decision.stop_reason,
                     1 if decision.shadow else 0,
                     ",".join(decision.completed_layers),
                     decision.created_at or now_iso(),
                     _dumps(decision.to_dict())])
                self._project(conn, decision)
        except sqlite3.Error as exc:
            raise CompletionStoreError("decision",
                                       f"could not store {decision.id}",
                                       got=str(exc)) from exc
        return decision.id

    @staticmethod
    def _reject_repeats(field: str, ids: Sequence[str]) -> None:
        """Refuse a decision whose candidate rows would overwrite each other.

        Two candidates sharing an id project onto one row and the second wins,
        which loses an improvement -- or, worse, loses a REJECTION, since the
        two lists share this table and a rejected candidate that also appears as
        executed would quietly stop being counted as refused.
        """
        seen = set()
        for value in ids:
            if value in seen:
                raise CompletionStoreError(
                    f"{field}[].id",
                    "appears twice in this decision; two candidates with one id "
                    "project onto one row and the second would erase the first",
                    got=value)
            seen.add(value)

    @staticmethod
    def _project(conn: sqlite3.Connection, decision: CompletionDecision) -> None:
        """Rewrite the denormalised rows for this decision, in the payload's own
        transaction.

        These rows are a CONVENIENCE. The payload is the decision. Anything that
        reads them and reports the result is reporting a summary of a document it
        did not open.
        """
        conn.execute("DELETE FROM completion_candidates WHERE decision_id = ?",
                     [decision.id])
        conn.executemany(
            "INSERT INTO completion_candidates (decision_id, candidate_id, layer, "
            "category, relation, source, status, rejection_reason, expected_value, "
            "estimated_cost, risk) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [(decision.id, c.id, c.layer, c.category, c.relation, c.source,
              c.status, c.rejection_reason, float(c.expected_value),
              float(c.estimated_cost), float(c.risk))
             for c in (decision.executed + decision.rejected + decision.deferred)])
        conn.execute("DELETE FROM completion_spends WHERE decision_id = ?",
                     [decision.id])
        conn.executemany(
            "INSERT INTO completion_spends (decision_id, line, rounds, tool_calls, "
            "tokens, seconds) VALUES (?,?,?,?,?,?)",
            [(decision.id, str(line), int(spend.rounds), int(spend.tool_calls),
              int(spend.tokens), float(spend.seconds))
             for line, spend in sorted(decision.budget.spent.items())])

    def get_decision(self, decision_id: str, *,
                     owner: str) -> Optional[CompletionDecision]:
        """One decision by id, shadow ones included.

        By ID there is no mixing to prevent: the caller already named the row it
        wants, and `decision.shadow` on the object it gets back says which kind
        it is. It is the reads that AGGREGATE that have to ask -- one row is
        never a statistic.
        """
        clause, params = self._owner_clause(owner)
        row = self._one(
            f"SELECT payload FROM completion_decisions WHERE id = ? AND {clause}",
            [str(decision_id)] + params)
        return self._decision(row) if row else None

    def decision_for_run(self, run_id: str, *, owner: str,
                         shadow: bool = False) -> Optional[CompletionDecision]:
        """The newest decision of that kind for that run.

        `shadow` is a real `bool` here and not `Optional[bool]`, and the missing
        third option is the point: one run can hold BOTH a real decision and the
        shadow measurement taken beside it, so "the decision for this run" is not
        a question until the caller says which of the two it means. Answering
        "whichever is newer" would hand back a prediction on the runs where the
        measurement finished last, and nothing in the returned object's shape
        makes the caller notice.
        """
        clause, params = self._owner_clause(owner)
        shade, shade_params = self._shadow_clause(bool(shadow))
        row = self._one(
            f"SELECT payload FROM completion_decisions WHERE run_id = ? "
            f"AND {clause} AND {shade} ORDER BY created_at DESC, rowid DESC LIMIT 1",
            [str(run_id)] + params + shade_params)
        return self._decision(row) if row else None

    def list_decisions(self, *, owner: str, shadow: Optional[bool] = False,
                       mode: str = "", project_id: str = "",
                       stop_reason: str = "", limit: int = 50,
                       cursor: str = "") -> List[CompletionDecision]:
        """One owner's decisions, newest first, one page at a time.

        `shadow` defaults to `False`, which is what really happened. `True`
        lists the measurements instead. `None` lists both and is FOR DIAGNOSTICS
        ONLY -- a page that mixes them shows the same run twice, once as it went
        and once as it was predicted to go, and the reader has no way to tell
        which row is which without opening every payload. Nothing that produces
        a number for a human should pass it.

        `cursor` is the opaque `created_at|id` of the last row of the previous
        page. Compound rather than an offset because decisions are inserted
        while a caller pages: an `OFFSET` would skip a row every time one
        arrived.
        """
        clause, params = self._owner_clause(owner)
        shade, shade_params = self._shadow_clause(shadow)
        where = [clause, shade]
        params = params + shade_params
        if mode:
            where.append("mode = ?")
            params.append(str(mode))
        if project_id:
            where.append("project_id = ?")
            params.append(str(project_id))
        if stop_reason:
            where.append("stop_reason = ?")
            params.append(str(stop_reason))
        after_at, _, after_id = str(cursor or "").partition("|")
        if cursor and not after_id:
            # Never raise on a read: start from the top and say so, because a
            # cursor that cannot be read is a caller bug, and a page silently
            # served from the wrong place is the same bug made invisible.
            logger.warning("completion engine: unreadable list cursor %r; the "
                           "page starts from the newest row", cursor)
        elif after_id:
            where.append("(created_at < ? OR (created_at = ? AND id < ?))")
            params.extend([after_at, after_at, after_id])
        rows = self._all(
            f"SELECT payload FROM completion_decisions WHERE {' AND '.join(where)} "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            params + [_limit(limit, 50, 500)])
        return _kept(self._decision(r) for r in rows)

    @staticmethod
    def page_cursor(decision: CompletionDecision) -> str:
        """The `cursor` that resumes `list_decisions` after this decision.

        Built here rather than in the caller so that the encoding stays one
        thing: a route that assembled the string itself would keep working right
        up to the day the ordering changes.
        """
        return f"{decision.created_at}|{decision.id}"

    # -- counting ---------------------------------------------------------

    def counts(self, *, owner: str) -> Dict[str, Any]:
        """How much of this owner's account is here. Diagnostics and tests.

        `decisions` is reported SPLIT -- `real_decisions` beside
        `shadow_decisions` -- rather than as one number, for the reason at the
        top of this file: one total is exactly the blend this store exists to
        prevent, and a caller reading it would be reading a count of runs that
        half happened. The table's row count is kept beside them under
        `decisions`, because "how big is this file" is a different question from
        "how many runs were there" and only the first one wants a sum.

        The child tables are counted through a join rather than directly,
        because they carry no owner of their own -- they belong to the decision
        that owns them, and counting them unscoped would tell one user how busy
        another one is.

        A count that could not be taken is -1 and never 0: "we could not look"
        and "there is nothing" are different answers, and only one of them is a
        reason to stop worrying.
        """
        clause, params = self._owner_clause(owner)
        joined, _ = self._owner_clause(owner, alias="d")
        out: Dict[str, Any] = {}
        for key, sql in (
            ("scopes",
             f"SELECT count(*) AS n FROM completion_scopes WHERE {clause}"),
            ("contracts",
             f"SELECT count(*) AS n FROM completion_contracts WHERE {clause}"),
            ("decisions",
             f"SELECT count(*) AS n FROM completion_decisions WHERE {clause}"),
            ("real_decisions",
             f"SELECT count(*) AS n FROM completion_decisions WHERE shadow = 0 "
             f"AND {clause}"),
            ("shadow_decisions",
             f"SELECT count(*) AS n FROM completion_decisions WHERE shadow = 1 "
             f"AND {clause}"),
            ("candidates",
             "SELECT count(*) AS n FROM completion_candidates c JOIN "
             f"completion_decisions d ON d.id = c.decision_id WHERE {joined}"),
            ("spends",
             "SELECT count(*) AS n FROM completion_spends s JOIN "
             f"completion_decisions d ON d.id = s.decision_id WHERE {joined}"),
        ):
            row = self._one(sql, params)
            out[key] = int(row["n"]) if row else -1
        return out

    # -- what a person said no to ------------------------------------------

    def record_refusal(self, *, owner: str, candidate_key: str, reason: str,
                       decision_id: str = "", candidate_id: str = "",
                       project_id: str = "", actor: str = "") -> bool:
        """A person declined one improvement, with a reason. §12.

        Returns whether it was written, and the caller is expected to look:
        this is the one write in the whole HTTP surface, and a route that
        answered `ok: true` after storing nothing is exactly the bug that made
        this method necessary -- the person's answer existed only in the
        response body, and the same candidate came back the next round having
        lost nothing but their refusal.

        `owner` and `reason` are both required and neither is defaulted. An
        unowned refusal would apply to everybody, which is not what anybody
        said; and a refusal nobody justified cannot be told, a month later,
        from one nobody meant -- which is why §1.8's "must not reappear without
        new evidence" needs the sentence and not just the flag.

        The write REPLACES a previous refusal of the same improvement rather
        than accumulating: the interesting fact is that this owner's answer is
        no and what their latest reason for it was, and a table that grew a row
        every time someone re-declined would make the newest reason the hardest
        one to find.
        """
        who = str(owner or "").strip()
        key = str(candidate_key or "").strip()
        why = str(reason or "").strip()
        if not who or not key or not why:
            return False
        try:
            with self._db() as conn:
                conn.execute(
                    "INSERT INTO completion_refusals "
                    "(owner, candidate_key, decision_id, candidate_id, project_id, "
                    " reason, actor, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(owner, candidate_key) DO UPDATE SET "
                    "decision_id=excluded.decision_id, "
                    "candidate_id=excluded.candidate_id, "
                    "project_id=excluded.project_id, reason=excluded.reason, "
                    "actor=excluded.actor, at=excluded.at",
                    [who, key, str(decision_id or ""), str(candidate_id or ""),
                     str(project_id or ""), why, str(actor or who), now_iso()])
                conn.commit()
            return True
        except Exception:  # noqa: BLE001 - the route must not 500 on a write
            logger.exception("completion engine: recording a refusal failed")
            return False

    def declined_keys(self, *, owner: str, project_id: str = "") -> Tuple[str, ...]:
        """Every `candidate.key` this owner has already said no to.

        Fed into `frontier.build` so the refusal is enforced where admission is
        decided, and not somewhere later where a high-value candidate could
        argue with it. Reads never raise: a refusal table that cannot be read
        must cost a repeated question, never the turn.

        `project_id` widens rather than narrows when it is empty -- a refusal
        recorded with no project is a refusal everywhere, because that is what
        a person with no project open was answering about.
        """
        who = str(owner or "").strip()
        if not who:
            return ()
        wanted = str(project_id or "").strip()
        if wanted:
            rows = self._all(
                "SELECT candidate_key FROM completion_refusals WHERE owner = ? "
                "AND (project_id = '' OR project_id = ?)", [who, wanted])
        else:
            rows = self._all(
                "SELECT candidate_key FROM completion_refusals WHERE owner = ?",
                [who])
        return tuple(str(row["candidate_key"]) for row in rows if row["candidate_key"])

    def refusals(self, *, owner: str, decision_id: str = "") -> List[Dict[str, Any]]:
        """The refusals themselves, for the page that shows what was turned down."""
        who = str(owner or "").strip()
        if not who:
            return []
        if decision_id:
            rows = self._all(
                "SELECT * FROM completion_refusals WHERE owner = ? AND decision_id = ? "
                "ORDER BY at DESC", [who, str(decision_id)])
        else:
            rows = self._all(
                "SELECT * FROM completion_refusals WHERE owner = ? ORDER BY at DESC "
                "LIMIT 500", [who])
        return [dict(row) for row in rows]

    def rejection_stats(self, *, owner: str,
                        shadow: Optional[bool] = False) -> Dict[str, int]:
        """How many candidates were refused, by reason. Reads never raise.

        This is the read the `completion_candidates` projection exists for. "How
        many improvements did we refuse for `budget` this month" is the number
        that decides whether a budget is too small, and a question that costs a
        JSON parse of every payload in the table is a question nobody asks
        twice.

        `shadow` defaults to `False` -- what really happened. `True` counts the
        measurements, and `None` counts both TOGETHER and is for diagnostics
        only: a refusal a shadow run predicted is not a refusal anybody
        suffered, and adding the two produces a rate that describes no run that
        ever existed.

        Only the reasons that actually occur appear. A dictionary padded with
        every `REJECTION_REASONS` at zero would read as a survey of what this
        owner's runs refuse, and most of those zeroes would mean "never
        happened" rather than "measured at none".
        """
        clause, params = self._owner_clause(owner, alias="d")
        shade, shade_params = self._shadow_clause(shadow, alias="d")
        rows = self._all(
            "SELECT c.rejection_reason AS reason, count(*) AS n FROM "
            "completion_candidates c JOIN completion_decisions d "
            f"ON d.id = c.decision_id WHERE {clause} AND {shade} "
            "AND c.rejection_reason <> '' GROUP BY c.rejection_reason",
            params + shade_params)
        return {str(row["reason"]): int(row["n"]) for row in rows}

    def spend_stats(self, *, owner: str, shadow: Optional[bool] = False
                    ) -> Dict[str, Dict[str, float]]:
        """What was spent, per budget LINE and per unit. Reads never raise.

        Per line and not per pot. `FUNDING_LINES` says that `recovery` and
        `core` come out of one pot and that `exploration` spends the bonus
        share, and that is the right answer to "may I spend this?"; it is the
        wrong answer to "what did this run spend it on". The caller said the
        spend was recovery work and the closeout reports it as recovery work, so
        the account keeps the line the caller named. Anyone who wants the pots
        can fold the lines with `FUNDING_LINES`; nobody can unfold them again
        once this method has done it for them.

        `shadow` defaults to `False`, and the mixing rule bites hardest right
        here: a shadow run spends NOTHING -- it records what it would have spent
        -- so adding its lines to the real ones inflates the cost of work that
        was done with the cost of work that was only imagined, and the total is
        then wrong in the one direction that makes a budget look too small when
        it was not. `None` is for diagnostics and is documented as such.

        `seconds` comes back as a float and the other three as floats too, so
        that a caller summing them does not have to know which unit sqlite chose
        to widen.
        """
        clause, params = self._owner_clause(owner, alias="d")
        shade, shade_params = self._shadow_clause(shadow, alias="d")
        rows = self._all(
            "SELECT s.line AS line, sum(s.rounds) AS rounds, "
            "sum(s.tool_calls) AS tool_calls, sum(s.tokens) AS tokens, "
            "sum(s.seconds) AS seconds FROM completion_spends s "
            "JOIN completion_decisions d ON d.id = s.decision_id "
            f"WHERE {clause} AND {shade} GROUP BY s.line",
            params + shade_params)
        out: Dict[str, Dict[str, float]] = {}
        for row in rows:
            out[str(row["line"])] = {
                "rounds": float(row["rounds"] or 0),
                "tool_calls": float(row["tool_calls"] or 0),
                "tokens": float(row["tokens"] or 0),
                "seconds": round(float(row["seconds"] or 0.0), 3),
            }
        return out

    # -- readers ----------------------------------------------------------
    #
    # Both swallow and log. A read that raised on the turn path would let the
    # account of a run break the run it was accounting for.

    def _one(self, sql: str, params: Sequence[Any]) -> Optional[sqlite3.Row]:
        try:
            with self._db() as conn:
                return conn.execute(sql, list(params)).fetchone()
        except Exception:  # noqa: BLE001 - reads never raise
            logger.exception("completion engine: a read failed: %s",
                             sql.split(" WHERE ")[0])
            return None

    def _all(self, sql: str, params: Sequence[Any]) -> List[sqlite3.Row]:
        try:
            with self._db() as conn:
                return list(conn.execute(sql, list(params)).fetchall())
        except Exception:  # noqa: BLE001 - reads never raise
            logger.exception("completion engine: a read failed: %s",
                             sql.split(" WHERE ")[0])
            return []

    # -- row -> contract ---------------------------------------------------
    #
    # A payload that will not parse is logged and skipped rather than raised: one
    # damaged row must not make a whole page unreadable, and the row is still in
    # the file for whoever wants to look at it.
    #
    # `ContractError` and not `CompletionError`. The second is a SUBCLASS of the
    # first, and `parse()` reaches the shared helpers in `src/contracts/base.py`
    # -- `text`, `one_of`, `timestamp` -- which raise the PARENT. Catching only
    # the child meant a single damaged row raised straight out of a read and took
    # the whole of `GET /api/completion` with it, which is the opposite of what
    # the paragraph above promises. Same mistake as the delta routes made one
    # plan ago, one layer further down.

    @staticmethod
    def _scope(row: sqlite3.Row) -> Optional[ScopeEnvelope]:
        try:
            return ScopeEnvelope.parse(_loads(row["payload"], {}))
        except ContractError:
            logger.exception("completion engine: a scope payload will not parse")
            return None

    @staticmethod
    def _contract(row: sqlite3.Row) -> Optional[CompletionContract]:
        try:
            return CompletionContract.parse(_loads(row["payload"], {}))
        except ContractError:
            logger.exception("completion engine: a contract payload will not parse")
            return None

    @staticmethod
    def _decision(row: sqlite3.Row) -> Optional[CompletionDecision]:
        try:
            return CompletionDecision.parse(_loads(row["payload"], {}))
        except ContractError:
            logger.exception("completion engine: a decision payload will not parse")
            return None
