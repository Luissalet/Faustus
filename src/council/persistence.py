"""
council/persistence.py — the room survives the process, or it was never a room.

A council is the first thing in Faustus that a page reload can bankrupt.  A
tournament run is one call and its result; a dispatch run reports to a worker
that owns it.  A council holds a decision three models argued about, a claim on
a file somebody is halfway through editing, a blocking objection nobody has
answered and a turn that was mid-flight when the machine lost power — and §15.1
is blunt about the consequence: this store is the single source of truth for
operational state, not a cache in front of somebody's memory.

Four failures shaped what is here:

* **A retried POST paid twice.**  `claim_idempotency()` is the anti-duplicate
  mechanism (§15.3, §20): the first caller with a key gets `(True, ref)`, every
  caller after it gets `(False, the same ref)`, and the turns table carries a
  unique index on `(session_id, idempotency_key)` so the guarantee survives two
  processes racing, not just two threads.

* **A blind round that was not blind.**  `list_messages(viewer_id=…)` filters
  by visibility and audience.  If the filter lived in a route, the second route
  would forget it, and a `consult` round where one model saw another's answer is
  a round that produced one opinion wearing two names.

* **Two contradictory orders both applied.**  `update_session` takes
  `expected_revision` and raises `RevisionConflict` naming the revision that was
  actually there (§8).  Last-write-wins loses the first write silently, which is
  the worst way to lose a `pause`.

* **A restart repeated an effect.**  `recover()` marks interrupted turns,
  releases claims whose holder died with the process, reports the tasks that
  need reconciling with Dispatch — and re-runs nothing (§15.2, §25).

The SQLite policy is `src/context_engine/store.py`'s, deliberately: its own
file, one `threading.RLock`, WAL, short-lived connections, quarantine instead of
deletion when the file is unreadable, and schema registered per feature so a
module owns its tables.  The reason is the same one that argued for
`context_engine.db`: an eight-second maintenance pass here must never hold a
write lock that a chat turn is waiting on.

One asymmetry runs through the whole file and is on purpose (§20): **reads
never raise**.  A read that throws on the turn path turns a diagnostic into a
dead conversation, so every getter answers `None`, `[]` or `{}` and logs.
Writes raise typed errors, and the routes map them.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Type

from src.constants import DATA_DIR
from src.contracts.base import ContractError, as_mapping

from .contracts import (
    ACTIVE_CLAIM_STATES,
    DECISION_TRANSITIONS,
    SESSION_TRANSITIONS,
    TRANSITIONS,
    TURN_INTERRUPTED,
    TURN_STATES_AT_REST,
    CouncilClaim,
    CouncilDecision,
    CouncilError,
    CouncilMessage,
    CouncilObjection,
    CouncilParticipant,
    CouncilSession,
    CouncilTask,
    CouncilTurn,
    check_transition,
    new_id,
    supersede,
)

logger = logging.getLogger(__name__)

#: Where the council's state lives.
#:
#: Defined here rather than in `src/constants.py` on purpose *for now*: this
#: module is landing on its own branch beside three others, and touching the
#: shared constants file would put a merge conflict between four agents for one
#: line.  It belongs in `src/constants.py` next to `CONTEXT_ENGINE_DB`, and the
#: hand-off note says so — the value is the one it would have there, so lifting
#: it is a one-line move and an import change.
#:
#: Why a separate file at all, when `app.db` exists: unlike the Context Engine's
#: database this one is NOT rebuildable — it is the record of what a room
#: decided — but it is written on every turn by several participants at once,
#: and a room with four models talking must not be able to block a chat turn on
#: a write lock.  Separate file, separate lock, separate blast radius.
COUNCIL_DB = os.path.join(DATA_DIR, "council.db")

__all__ = [
    "COUNCIL_DB", "CouncilStoreError", "NotFound", "RevisionConflict",
    "ClaimConflict", "DuplicateTurn", "CouncilStore", "register_schema",
    "registered_schemas", "db_path", "use_path", "store", "now_iso",
]


# ── errors.  Every one of them says which row and what was there ───────────

class CouncilStoreError(CouncilError):
    """A write could not be made.  Routes map this to a 400/500.

    It is a `CouncilError`, hence a `ContractError`, hence a `ValueError`: a
    caller that already catches contract failures keeps working."""

    def __init__(self, path: str, message: str, *, got: Any = None) -> None:
        if got is None:
            super().__init__(path, message)
        else:
            super().__init__(path, message, got=got)


class NotFound(CouncilStoreError):
    """The row is not there, or not there *for this owner* — the two are the
    same answer on purpose (§20: another owner's session is not visible, and
    "forbidden" would confirm it exists)."""

    def __init__(self, kind: str, row_id: str) -> None:
        self.kind = str(kind)
        self.row_id = str(row_id)
        super().__init__(kind, f"{kind} {row_id!r} does not exist here")


class RevisionConflict(CouncilStoreError):
    """`expected_revision` did not match.  Carries `revision`, the one that was
    actually stored, so the caller can re-read, merge and retry instead of
    guessing — the same posture as `context_engine.blocks.BlockConflict`."""

    def __init__(self, session_id: str, expected: Optional[int], actual: int) -> None:
        self.session_id = str(session_id)
        self.expected = expected
        self.revision = int(actual)
        super().__init__(
            "session.revision",
            f"session {self.session_id} is at revision {self.revision}, not "
            f"{expected}; re-read it and re-apply your change",
        )


class ClaimConflict(CouncilStoreError):
    """Somebody else already holds this resource (§3.3).

    Carries `holder_id` and `claim_id`, because "conflict" tells a driver
    nothing and "p_codex holds src/auth/github.py" tells it who to ask."""

    def __init__(self, resource: str, holder_id: str, claim_id: str) -> None:
        self.resource = str(resource)
        self.holder_id = str(holder_id)
        self.claim_id = str(claim_id)
        super().__init__(
            "claim.resource",
            f"{self.resource!r} is already held by {self.holder_id or 'another participant'} "
            f"(claim {self.claim_id}); one resource has one owner at a time",
        )


class DuplicateTurn(CouncilStoreError):
    """This idempotency key already made a turn in this room (§15.3).

    Carries `turn_id`, so the retry can be answered with the turn the first
    request created instead of being told to try again — which is what a client
    that lost its response actually wants."""

    def __init__(self, key: str, turn_id: str) -> None:
        self.key = str(key)
        self.turn_id = str(turn_id)
        super().__init__(
            "turn.idempotency_key",
            f"{self.key!r} already opened turn {self.turn_id} in this session; "
            "a retry answers with that turn, it does not open a second one",
        )


# ── module state: one path, one lock, schemas registered per feature ───────

_LOCK = threading.RLock()
_SCHEMAS: Dict[str, Tuple[str, ...]] = {}
_PATH_OVERRIDE: Optional[str] = None
_STORE: Optional["CouncilStore"] = None


def register_schema(name: str, statements: Sequence[str]) -> None:
    """Declare the tables one feature owns.

    Every statement must be idempotent (`CREATE … IF NOT EXISTS`); they run on
    every connection, which costs microseconds and removes the whole class of
    "the table did not exist yet".  Re-registering a name replaces it, so a
    module reload in a test suite is harmless.

    `ledger.py`, `events.py` and `scheduler.py` are expected to call this with
    their own tables rather than adding columns here."""
    with _LOCK:
        _SCHEMAS[str(name)] = tuple(str(s) for s in statements if str(s).strip())


def registered_schemas() -> Tuple[str, ...]:
    with _LOCK:
        return tuple(sorted(_SCHEMAS))


def db_path() -> str:
    return _PATH_OVERRIDE or COUNCIL_DB


def use_path(path: Optional[str]) -> None:
    """Point the store somewhere else, and drop the shared instance.

    Only tests and the doctor call this.  It exists so a test can use
    `tmp_path` without monkeypatching a constant other modules already imported
    by value — and it resets `_STORE` because a store built against the old
    path would otherwise outlive the override."""
    global _PATH_OVERRIDE, _STORE
    with _LOCK:
        _PATH_OVERRIDE = path
        _STORE = None


def store() -> "CouncilStore":
    """The process-wide store.  Built on first use, dropped by `use_path`."""
    global _STORE
    with _LOCK:
        if _STORE is None:
            _STORE = CouncilStore()
        return _STORE


# ── small shared helpers ───────────────────────────────────────────────────

def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _stamp(now: Any) -> str:
    """The timestamp a caller supplied, or this moment.

    `recover(now=…)` takes one so a test can pin it; an unreadable value falls
    back to now rather than being written as-is, because a clock nobody can
    parse in the recovery report is worse than an approximate one."""
    if now is None:
        return now_iso()
    if isinstance(now, datetime):
        moment = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).replace(
            microsecond=0).isoformat().replace("+00:00", "Z")
    text_value = str(now).strip()
    try:
        parsed = datetime.fromisoformat(text_value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        logger.warning("council: unreadable `now` %r; using the current time", now)
        return now_iso()
    return _stamp(parsed)


def _dumps(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return "null"


def _loads(raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _loads_list(raw: Any) -> List[Any]:
    value = _loads(raw, [])
    return value if isinstance(value, list) else []


def _loads_dict(raw: Any) -> Dict[str, Any]:
    value = _loads(raw, {})
    return value if isinstance(value, dict) else {}


def _encode(value: Any) -> Any:
    """A tuple or a dict becomes JSON; everything else goes in as it is.

    Booleans are the one thing worth naming: SQLite stores them as 0/1 anyway,
    and letting sqlite3 do the conversion keeps `True` from arriving back as
    the string "True" through a round trip nobody looked at."""
    if isinstance(value, (list, tuple, dict)):
        return _dumps(list(value) if isinstance(value, tuple) else value)
    if isinstance(value, bool):
        return 1 if value else 0
    return value


def _values(payload: Mapping[str, Any], columns: Sequence[str]) -> Tuple[Any, ...]:
    return tuple(_encode(payload.get(column)) for column in columns)


def _insert_sql(table: str, columns: Sequence[str]) -> str:
    return (f"INSERT INTO {table} (" + ", ".join(columns) + ") VALUES ("
            + ", ".join("?" for _ in columns) + ")")


def _update_sql(table: str, columns: Sequence[str], *, key: Sequence[str] = ("id",)) -> str:
    assigned = [c for c in columns if c not in key]
    return (f"UPDATE {table} SET " + ", ".join(f"{c} = ?" for c in assigned)
            + " WHERE " + " AND ".join(f"{k} = ?" for k in key))


def _owner_clause(owner: str = "", *, alias: str = "") -> Tuple[str, List[Any]]:
    """The visibility filter, written once.

    An empty `owner` is the UNSCOPED read: recovery, maintenance, the doctor and
    tests use it.  Every route passes the signed-in owner, and §20 is the reason
    — another owner's room must not be listable, readable or countable."""
    prefix = f"{alias}." if alias else ""
    if not str(owner or "").strip():
        return "1 = 1", []
    return f"{prefix}owner = ?", [str(owner)]


def _quarantine(path: str, reason: Any) -> None:
    """Move an unreadable database aside rather than deleting it.

    Unlike the Context Engine's file this one is not rebuildable, which makes
    keeping the `.corrupt` copy more important, not less: `sqlite3 .recover`
    succeeds more often than people expect, and the alternative is telling
    somebody the decisions their room reached are simply gone."""
    for suffix in ("", "-wal", "-shm"):
        victim = path + suffix
        if not os.path.exists(victim):
            continue
        try:
            os.replace(victim, victim + ".corrupt")
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(victim)
    logger.error("council.db was unusable (%s); moved aside as .corrupt and recreated", reason)


def _apply_schema(conn: sqlite3.Connection) -> None:
    with _LOCK:
        blocks = list(_SCHEMAS.items())
    for name, statements in blocks:
        for statement in statements:
            try:
                conn.execute(statement)
            except sqlite3.Error as exc:
                # One feature's bad DDL must not take the store down with it;
                # that feature fails loudly on its first query instead.
                logger.error("council schema %s failed: %s", name, exc)


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10.0)
    try:
        conn.row_factory = sqlite3.Row
        with contextlib.suppress(sqlite3.Error):
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
        # Probe before touching the schema, and NOT inside the suppression
        # above.  `sqlite3.connect` is lazy and `_apply_schema` swallows a
        # failing statement per feature, so without this a file that is not a
        # database at all would open "successfully" and then fail on every
        # single query — quarantine would never run and the store would look
        # merely empty, which is the worst possible way to lose a ledger.
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        _apply_schema(conn)
        conn.commit()
        return conn
    except Exception:
        with contextlib.suppress(Exception):
            conn.close()
        raise


# ── schema ─────────────────────────────────────────────────────────────────
#
# `owner` lives on the sessions table only, and every other table reaches it
# through `session_id`.  That is a deliberate choice against denormalising the
# owner everywhere: two copies of an owner is two chances for a scoped query to
# read the stale one, and the price is one join in `list_sessions`-shaped reads.
#
# Two indexes are load-bearing invariants rather than performance work:
#
# * `idx_council_turn_idem` makes "the same idempotency key never makes two
#   turns" true across processes, not merely inside this one's lock;
# * `idx_council_claim_active` makes "one active claim per resource" a property
#   of the database, so two drivers racing produce a `ClaimConflict` and not two
#   holders (§3.3, §20 "Claims y herramientas").

register_schema("council", (
    """
    CREATE TABLE IF NOT EXISTS council_sessions (
        id                       TEXT PRIMARY KEY,
        owner                    TEXT NOT NULL DEFAULT '',
        title                    TEXT NOT NULL DEFAULT '',
        policy                   TEXT NOT NULL DEFAULT 'chat',
        status                   TEXT NOT NULL DEFAULT 'draft',
        phase                    TEXT NOT NULL DEFAULT '',
        parent_session_id        TEXT NOT NULL DEFAULT '',
        workspace                TEXT NOT NULL DEFAULT '',
        project_id               TEXT NOT NULL DEFAULT '',
        participants             TEXT NOT NULL DEFAULT '[]',
        budgets                  TEXT NOT NULL DEFAULT '{}',
        activity_completion_mode TEXT NOT NULL DEFAULT '',
        created_at               TEXT NOT NULL DEFAULT '',
        updated_at               TEXT NOT NULL DEFAULT '',
        revision                 INTEGER NOT NULL DEFAULT 1,
        archived                 INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_council_sessions_owner "
    "ON council_sessions(owner, archived, updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_council_sessions_status "
    "ON council_sessions(status)",
    """
    CREATE TABLE IF NOT EXISTS council_participants (
        session_id         TEXT NOT NULL,
        id                 TEXT NOT NULL,
        display_name       TEXT NOT NULL DEFAULT '',
        kind               TEXT NOT NULL DEFAULT 'model',
        model              TEXT NOT NULL DEFAULT '',
        endpoint_id        TEXT NOT NULL DEFAULT '',
        agent_slug         TEXT NOT NULL DEFAULT '',
        roles              TEXT NOT NULL DEFAULT '[]',
        tool_profile       TEXT NOT NULL DEFAULT 'none',
        status             TEXT NOT NULL DEFAULT 'available',
        private_session_id TEXT NOT NULL DEFAULT '',
        capabilities       TEXT NOT NULL DEFAULT '[]',
        resolution_id      TEXT NOT NULL DEFAULT '',
        completion_mode    TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (session_id, id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS council_messages (
        seq          INTEGER PRIMARY KEY AUTOINCREMENT,
        id           TEXT NOT NULL UNIQUE,
        session_id   TEXT NOT NULL DEFAULT '',
        turn_id      TEXT NOT NULL DEFAULT '',
        author_id    TEXT NOT NULL DEFAULT '',
        author_kind  TEXT NOT NULL DEFAULT 'model',
        audience     TEXT NOT NULL DEFAULT '[]',
        message_type TEXT NOT NULL DEFAULT 'message',
        content      TEXT NOT NULL DEFAULT '',
        reply_to     TEXT NOT NULL DEFAULT '',
        task_id      TEXT NOT NULL DEFAULT '',
        decision_id  TEXT NOT NULL DEFAULT '',
        visibility   TEXT NOT NULL DEFAULT 'room',
        created_at   TEXT NOT NULL DEFAULT '',
        metadata     TEXT NOT NULL DEFAULT '{}'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_council_messages_session "
    "ON council_messages(session_id, seq)",
    "CREATE INDEX IF NOT EXISTS idx_council_messages_turn "
    "ON council_messages(turn_id)",
    """
    CREATE TABLE IF NOT EXISTS council_turns (
        id                    TEXT PRIMARY KEY,
        session_id            TEXT NOT NULL DEFAULT '',
        state                 TEXT NOT NULL DEFAULT 'received',
        author_id             TEXT NOT NULL DEFAULT '',
        content               TEXT NOT NULL DEFAULT '',
        policy                TEXT NOT NULL DEFAULT 'chat',
        selected_participants TEXT NOT NULL DEFAULT '[]',
        idempotency_key       TEXT NOT NULL DEFAULT '',
        stop_reason           TEXT NOT NULL DEFAULT '',
        created_at            TEXT NOT NULL DEFAULT '',
        updated_at            TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_council_turns_session "
    "ON council_turns(session_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_council_turns_state ON council_turns(state)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_council_turn_idem "
    "ON council_turns(session_id, idempotency_key) WHERE idempotency_key <> ''",
    """
    CREATE TABLE IF NOT EXISTS council_tasks (
        id                     TEXT PRIMARY KEY,
        session_id             TEXT NOT NULL DEFAULT '',
        title                  TEXT NOT NULL DEFAULT '',
        instruction            TEXT NOT NULL DEFAULT '',
        owner_participant_id   TEXT NOT NULL DEFAULT '',
        reviewer_participant_id TEXT NOT NULL DEFAULT '',
        status                 TEXT NOT NULL DEFAULT 'pending',
        depends_on             TEXT NOT NULL DEFAULT '[]',
        claimed_resources      TEXT NOT NULL DEFAULT '[]',
        acceptance             TEXT NOT NULL DEFAULT '[]',
        run_id                 TEXT NOT NULL DEFAULT '',
        proof_id               TEXT NOT NULL DEFAULT '',
        created_at             TEXT NOT NULL DEFAULT '',
        updated_at             TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_council_tasks_session "
    "ON council_tasks(session_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_council_tasks_owner "
    "ON council_tasks(owner_participant_id)",
    """
    CREATE TABLE IF NOT EXISTS council_claims (
        id          TEXT PRIMARY KEY,
        session_id  TEXT NOT NULL DEFAULT '',
        kind        TEXT NOT NULL DEFAULT 'file',
        resource    TEXT NOT NULL DEFAULT '',
        holder_id   TEXT NOT NULL DEFAULT '',
        state       TEXT NOT NULL DEFAULT 'requested',
        task_id     TEXT NOT NULL DEFAULT '',
        acquired_at TEXT NOT NULL DEFAULT '',
        expires_at  TEXT NOT NULL DEFAULT '',
        note        TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_council_claims_session "
    "ON council_claims(session_id, state)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_council_claim_active "
    "ON council_claims(session_id, kind, resource) "
    "WHERE state IN ('requested', 'held', 'handoff_pending')",
    """
    CREATE TABLE IF NOT EXISTS council_objections (
        id                  TEXT PRIMARY KEY,
        session_id          TEXT NOT NULL DEFAULT '',
        target_kind         TEXT NOT NULL DEFAULT 'message',
        target_id           TEXT NOT NULL DEFAULT '',
        author_id           TEXT NOT NULL DEFAULT '',
        severity            TEXT NOT NULL DEFAULT 'concern',
        claim               TEXT NOT NULL DEFAULT '',
        evidence_refs       TEXT NOT NULL DEFAULT '[]',
        proposed_resolution TEXT NOT NULL DEFAULT '',
        status              TEXT NOT NULL DEFAULT 'open',
        created_at          TEXT NOT NULL DEFAULT '',
        resolved_at         TEXT NOT NULL DEFAULT '',
        resolution_note     TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_council_objections_session "
    "ON council_objections(session_id, status, severity)",
    "CREATE INDEX IF NOT EXISTS idx_council_objections_target "
    "ON council_objections(target_id)",
    """
    CREATE TABLE IF NOT EXISTS council_decisions (
        id            TEXT PRIMARY KEY,
        session_id    TEXT NOT NULL DEFAULT '',
        question      TEXT NOT NULL DEFAULT '',
        status        TEXT NOT NULL DEFAULT 'proposed',
        chosen        TEXT NOT NULL DEFAULT '',
        alternatives  TEXT NOT NULL DEFAULT '[]',
        rationale     TEXT NOT NULL DEFAULT '[]',
        supporters    TEXT NOT NULL DEFAULT '[]',
        dissenters    TEXT NOT NULL DEFAULT '[]',
        evidence_refs TEXT NOT NULL DEFAULT '[]',
        supersedes    TEXT NOT NULL DEFAULT '',
        created_at    TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_council_decisions_session "
    "ON council_decisions(session_id, status)",
    """
    CREATE TABLE IF NOT EXISTS council_idempotency (
        session_id TEXT NOT NULL,
        key        TEXT NOT NULL,
        kind       TEXT NOT NULL DEFAULT '',
        ref        TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (session_id, key)
    )
    """,
))


# ── column maps ────────────────────────────────────────────────────────────
#
# The column order is the contract's field order; `_values()` reads the payload
# by name, so a mismatch is impossible rather than merely unlikely.  `_JSON`
# names the columns stored as JSON and the shape to read them back as, which is
# what keeps a corrupt cell from becoming a string where a list was expected.

_SESSION_COLUMNS: Tuple[str, ...] = (
    "id", "owner", "title", "policy", "status", "phase", "parent_session_id",
    "workspace", "project_id", "participants", "budgets",
    "activity_completion_mode", "created_at", "updated_at", "revision",
)
_SESSION_JSON = (("participants", list), ("budgets", dict))
_SESSION_MUTABLE: Tuple[str, ...] = (
    "title", "policy", "status", "phase", "parent_session_id", "workspace",
    "project_id", "participants", "budgets", "activity_completion_mode",
)

_PARTICIPANT_COLUMNS: Tuple[str, ...] = (
    "session_id", "id", "display_name", "kind", "model", "endpoint_id",
    "agent_slug", "roles", "tool_profile", "status", "private_session_id",
    "capabilities", "resolution_id", "completion_mode",
)
_PARTICIPANT_JSON = (("roles", list), ("capabilities", list))
_PARTICIPANT_MUTABLE: Tuple[str, ...] = (
    "display_name", "kind", "model", "endpoint_id", "agent_slug", "roles",
    "tool_profile", "status", "private_session_id", "capabilities",
    "resolution_id", "completion_mode",
)

_MESSAGE_COLUMNS: Tuple[str, ...] = (
    "id", "session_id", "turn_id", "author_id", "author_kind", "audience",
    "message_type", "content", "reply_to", "task_id", "decision_id",
    "visibility", "created_at", "metadata",
)
_MESSAGE_JSON = (("audience", list), ("metadata", dict))

_TURN_COLUMNS: Tuple[str, ...] = (
    "id", "session_id", "state", "author_id", "content", "policy",
    "selected_participants", "idempotency_key", "stop_reason",
    "created_at", "updated_at",
)
_TURN_JSON = (("selected_participants", list),)
#: `author_id` and `idempotency_key` are absent on purpose: rewriting either
#: would let a retry land on somebody else's turn.
_TURN_MUTABLE: Tuple[str, ...] = (
    "state", "content", "policy", "selected_participants", "stop_reason",
)

_TASK_COLUMNS: Tuple[str, ...] = (
    "id", "session_id", "title", "instruction", "owner_participant_id",
    "reviewer_participant_id", "status", "depends_on", "claimed_resources",
    "acceptance", "run_id", "proof_id", "created_at", "updated_at",
)
_TASK_JSON = (("depends_on", list), ("claimed_resources", list), ("acceptance", list))
_TASK_MUTABLE: Tuple[str, ...] = (
    "title", "instruction", "owner_participant_id", "reviewer_participant_id",
    "status", "depends_on", "claimed_resources", "acceptance", "run_id", "proof_id",
)

_CLAIM_COLUMNS: Tuple[str, ...] = (
    "id", "session_id", "kind", "resource", "holder_id", "state", "task_id",
    "acquired_at", "expires_at", "note",
)
_CLAIM_JSON: Tuple = ()
#: `kind` and `resource` are immutable: a claim is a claim *on one thing*, and
#: repointing it is how a handoff turns into a silent land grab (§11.3).
_CLAIM_MUTABLE: Tuple[str, ...] = (
    "state", "holder_id", "task_id", "acquired_at", "expires_at", "note",
)

_OBJECTION_COLUMNS: Tuple[str, ...] = (
    "id", "session_id", "target_kind", "target_id", "author_id", "severity",
    "claim", "evidence_refs", "proposed_resolution", "status", "created_at",
    "resolved_at", "resolution_note",
)
_OBJECTION_JSON = (("evidence_refs", list),)
#: The objection's own `claim` and `target` cannot change.  Answering an
#: objection is a status and a note; rewriting what was objected to is how a
#: resolved objection stops matching the thing it was about.
_OBJECTION_MUTABLE: Tuple[str, ...] = (
    "severity", "status", "evidence_refs", "proposed_resolution", "resolved_at",
    "resolution_note",
)

_DECISION_COLUMNS: Tuple[str, ...] = (
    "id", "session_id", "question", "status", "chosen", "alternatives",
    "rationale", "supporters", "dissenters", "evidence_refs", "supersedes",
    "created_at",
)
_DECISION_JSON = (("alternatives", list), ("rationale", list), ("supporters", list),
                  ("dissenters", list), ("evidence_refs", list))


def _parse_row(row: Mapping[str, Any], cls: Type, *,
               json_cols: Sequence[Tuple[str, type]] = (),
               drop: Sequence[str] = ()) -> Optional[Any]:
    """One row into one contract, or `None` with a warning.

    A row that cannot be parsed costs that row and nothing else.  Reads are on
    the turn path: one hand-edited cell must not be able to empty a room's
    entire transcript."""
    data = {k: v for k, v in dict(row).items() if k not in drop}
    for column, shape in json_cols:
        data[column] = (_loads_list(data.get(column)) if shape is list
                        else _loads_dict(data.get(column)))
    try:
        return cls.parse(data)
    except ContractError as exc:
        logger.warning("council: unusable %s row %s (%s); skipped",
                       cls.__name__, data.get("id"), exc)
        return None


def _merged(current: Mapping[str, Any], patch: Optional[Mapping[str, Any]],
            mutable: Sequence[str], *, label: str) -> Dict[str, Any]:
    """`current` with `patch` applied, refusing anything not on the list.

    Refusing rather than ignoring: a caller that thinks it just changed
    `created_at` and did not is a caller writing a bug report tomorrow."""
    try:
        changes = dict(as_mapping(patch if patch is not None else {}, f"{label}.patch"))
    except ContractError as exc:
        raise CouncilStoreError(f"{label}.patch", str(exc)) from exc
    unknown = sorted(k for k in changes if k not in mutable)
    if unknown:
        raise CouncilStoreError(
            f"{label}.patch",
            f"cannot update {', '.join(unknown)}; the updatable fields are {list(mutable)}",
        )
    merged = dict(current)
    merged.update(changes)
    return merged


# ── the store ──────────────────────────────────────────────────────────────

class CouncilStore:
    """Every durable fact about a council room.

    One instance per path.  The lock is the instance's, so two stores over two
    files never wait on each other, and `store()` hands out the shared one so
    that in the normal case there is exactly one lock over `council.db`."""

    def __init__(self, *, path: Optional[str] = None) -> None:
        #: `None` means "whatever `db_path()` says right now", which is what
        #: lets `use_path()` redirect a store that already exists.
        self._path = str(path) if path else None
        self._lock = threading.RLock()

    # ── plumbing ───────────────────────────────────────────────────────────

    @property
    def path(self) -> str:
        return self._path or db_path()

    def _open(self) -> sqlite3.Connection:
        target = self.path
        with contextlib.suppress(OSError):
            os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        try:
            return _connect(target)
        except sqlite3.DatabaseError as exc:
            _quarantine(target, exc)
            try:
                return _connect(target)
            except sqlite3.Error as exc2:  # pragma: no cover - dead disk
                raise CouncilStoreError("store", f"council store unusable: {exc2}") from exc2

    @contextlib.contextmanager
    def _db(self):
        """A short-lived connection under this store's lock, committed on
        success.  Short-lived on purpose: a long-held WAL connection keeps the
        `-wal` file growing and turns a crash into a slow recovery."""
        with self._lock:
            conn = self._open()
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

    def _fetch_one(self, sql: str, params: Sequence[Any], cls: Type, *,
                   json_cols: Sequence[Tuple[str, type]] = (),
                   drop: Sequence[str] = ()) -> Optional[Any]:
        try:
            with self._db() as conn:
                row = conn.execute(sql, tuple(params)).fetchone()
        except (sqlite3.Error, CouncilStoreError) as exc:
            logger.warning("council: read of %s failed: %s", cls.__name__, exc)
            return None
        if row is None:
            return None
        return _parse_row(row, cls, json_cols=json_cols, drop=drop)

    def _fetch_all(self, sql: str, params: Sequence[Any], cls: Type, *,
                   json_cols: Sequence[Tuple[str, type]] = (),
                   drop: Sequence[str] = ()) -> List[Any]:
        try:
            with self._db() as conn:
                rows = conn.execute(sql, tuple(params)).fetchall()
        except (sqlite3.Error, CouncilStoreError) as exc:
            logger.warning("council: listing of %s failed: %s", cls.__name__, exc)
            return []
        parsed = (_parse_row(r, cls, json_cols=json_cols, drop=drop) for r in rows)
        return [item for item in parsed if item is not None]

    @staticmethod
    def _as(cls: Type, value: Any, label: str):
        """Take the contract or the mapping the caller had to hand."""
        if isinstance(value, cls):
            return value
        try:
            return cls.parse(value)
        except ContractError as exc:
            raise CouncilStoreError(label, str(exc)) from exc

    def _insert(self, conn: sqlite3.Connection, table: str,
                columns: Sequence[str], payload: Mapping[str, Any], *,
                label: str, row_id: str) -> None:
        try:
            conn.execute(_insert_sql(table, columns), _values(payload, columns))
        except sqlite3.IntegrityError as exc:
            raise CouncilStoreError(label, f"{row_id!r} already exists ({exc})") from exc

    @staticmethod
    def _limit(value: Any, default: int, ceiling: int = 1000) -> int:
        try:
            wanted = int(value)
        except (TypeError, ValueError):
            wanted = default
        return max(1, min(wanted or default, ceiling))

    # ── sessions ───────────────────────────────────────────────────────────

    def create_session(self, session: CouncilSession) -> CouncilSession:
        """Store a new room.  Raises if its id is taken."""
        parsed = self._as(CouncilSession, session, "session")
        stamp = now_iso()
        payload = parsed.to_dict()
        payload["created_at"] = payload.get("created_at") or stamp
        payload["updated_at"] = payload.get("updated_at") or stamp
        stored = self._as(CouncilSession, payload, "session")
        try:
            with self._db() as conn:
                self._insert(conn, "council_sessions", _SESSION_COLUMNS,
                             stored.to_dict(), label="session", row_id=stored.id)
        except sqlite3.Error as exc:
            raise CouncilStoreError("session", f"could not store {stored.id}: {exc}") from exc
        return stored

    def get_session(self, session_id: str, *, owner: str = "") -> Optional[CouncilSession]:
        """The room, or `None` — including `None` when it belongs to somebody
        else.  Archived rooms are still returned: archiving hides a room from a
        listing, it does not delete the evidence it holds (§13)."""
        clause, params = _owner_clause(owner)
        return self._fetch_one(
            f"SELECT * FROM council_sessions WHERE id = ? AND {clause}",
            [str(session_id or "")] + params,
            CouncilSession, json_cols=_SESSION_JSON, drop=("archived",))

    def list_sessions(self, *, owner: str = "", status: str = "",
                      limit: int = 50) -> List[CouncilSession]:
        """Rooms this owner may see, most recently touched first."""
        clause, params = _owner_clause(owner)
        sql = f"SELECT * FROM council_sessions WHERE archived = 0 AND {clause}"
        if status:
            sql += " AND status = ?"
            params = params + [str(status)]
        sql += " ORDER BY updated_at DESC, id LIMIT ?"
        return self._fetch_all(sql, params + [self._limit(limit, 50)],
                               CouncilSession, json_cols=_SESSION_JSON,
                               drop=("archived",))

    def update_session(self, session_id: str, patch: Mapping[str, Any], *,
                       expected_revision: Optional[int] = None,
                       owner: str = "") -> CouncilSession:
        """Apply `patch`, check the status transition, bump the revision.

        `expected_revision` is the mechanism §8 asks for, not advice: a `pause`
        and a `set policy` that were both written against revision 6 cannot both
        land, and the loser is told the revision that actually won."""
        if expected_revision is not None and (isinstance(expected_revision, bool)
                                              or not isinstance(expected_revision, int)):
            raise CouncilStoreError(
                "session.expected_revision", "expected a whole number", got=expected_revision)
        clause, scope = _owner_clause(owner)
        with self._db() as conn:
            row = conn.execute(
                f"SELECT * FROM council_sessions WHERE id = ? AND {clause}",
                tuple([str(session_id or "")] + scope)).fetchone()
            if row is None:
                raise NotFound("session", session_id)
            current = _parse_row(row, CouncilSession, json_cols=_SESSION_JSON,
                                 drop=("archived",))
            if current is None:
                raise CouncilStoreError(
                    "session", f"session {session_id} is stored corrupt and cannot be updated")
            if expected_revision is not None and int(expected_revision) != current.revision:
                raise RevisionConflict(session_id, expected_revision, current.revision)
            merged = _merged(current.to_dict(), patch, _SESSION_MUTABLE, label="session")
            wanted = str(merged.get("status") or current.status)
            if wanted != current.status:
                check_transition(current.status, wanted, graph=SESSION_TRANSITIONS)
            merged["revision"] = current.revision + 1
            merged["updated_at"] = now_iso()
            updated = self._as(CouncilSession, merged, "session")
            conn.execute(_update_sql("council_sessions", _SESSION_COLUMNS),
                         _values(updated.to_dict(),
                                 [c for c in _SESSION_COLUMNS if c != "id"]) + (updated.id,))
        return updated

    def archive_session(self, session_id: str, *, owner: str = "") -> bool:
        """Hide a room from listings.  Nothing is deleted (§13: archive, never
        drop the execution and its evidence) and `get_session` still reads it."""
        clause, scope = _owner_clause(owner)
        try:
            with self._db() as conn:
                cur = conn.execute(
                    f"UPDATE council_sessions SET archived = 1, updated_at = ? "
                    f"WHERE id = ? AND {clause}",
                    tuple([now_iso(), str(session_id or "")] + scope))
                return bool(cur.rowcount)
        except sqlite3.Error as exc:
            raise CouncilStoreError(
                "session", f"could not archive {session_id}: {exc}") from exc


    # ── participants ───────────────────────────────────────────────────────

    def add_participant(self, session_id: str,
                        participant: CouncilParticipant) -> CouncilParticipant:
        """Seat a participant in a room.

        The participant contract carries no `session_id` — a resolved model is
        not owned by one room — so the seat is the (session, participant) row
        and the id only has to be unique inside the room."""
        parsed = self._as(CouncilParticipant, participant, "participant")
        payload = parsed.to_dict()
        payload["session_id"] = str(session_id or "")
        try:
            with self._db() as conn:
                self._insert(conn, "council_participants", _PARTICIPANT_COLUMNS,
                             payload, label="participant",
                             row_id=f"{payload['session_id']}/{parsed.id}")
        except sqlite3.Error as exc:
            raise CouncilStoreError(
                "participant", f"could not store {parsed.id}: {exc}") from exc
        return parsed

    def get_participant(self, session_id: str,
                        participant_id: str) -> Optional[CouncilParticipant]:
        return self._fetch_one(
            "SELECT * FROM council_participants WHERE session_id = ? AND id = ?",
            [str(session_id or ""), str(participant_id or "")],
            CouncilParticipant, json_cols=_PARTICIPANT_JSON, drop=("session_id",))

    def list_participants(self, session_id: str) -> List[CouncilParticipant]:
        return self._fetch_all(
            "SELECT * FROM council_participants WHERE session_id = ? ORDER BY id",
            [str(session_id or "")], CouncilParticipant,
            json_cols=_PARTICIPANT_JSON, drop=("session_id",))

    def update_participant(self, session_id: str, participant_id: str,
                           patch: Mapping[str, Any]) -> CouncilParticipant:
        """Change a seat: a role, a status, a narrowed tool profile.

        Narrowing is the common case and is why this exists (§11.1).  Widening
        is possible here and is checked nowhere in this file on purpose: the
        authority to grant a profile lives with the user, the project and the
        agent gate, and a store that pretended to enforce it would be a second,
        weaker opinion about permissions."""
        with self._db() as conn:
            row = conn.execute(
                "SELECT * FROM council_participants WHERE session_id = ? AND id = ?",
                (str(session_id or ""), str(participant_id or ""))).fetchone()
            if row is None:
                raise NotFound("participant", participant_id)
            current = _parse_row(row, CouncilParticipant, json_cols=_PARTICIPANT_JSON,
                                 drop=("session_id",))
            if current is None:
                raise CouncilStoreError(
                    "participant", f"participant {participant_id} is stored corrupt")
            merged = _merged(current.to_dict(), patch, _PARTICIPANT_MUTABLE,
                             label="participant")
            updated = self._as(CouncilParticipant, merged, "participant")
            payload = updated.to_dict()
            payload["session_id"] = str(session_id or "")
            assigned = [c for c in _PARTICIPANT_COLUMNS if c not in ("session_id", "id")]
            conn.execute(
                _update_sql("council_participants", _PARTICIPANT_COLUMNS,
                            key=("session_id", "id")),
                _values(payload, assigned) + (payload["session_id"], updated.id))
        return updated

    # ── messages (append-only) ─────────────────────────────────────────────
    #
    # There is no `update_message` and there will not be one.  A transcript
    # whose rows can be rewritten cannot support an audit, and a correction is
    # a new message with `reply_to` pointing at what it corrects.

    def append_message(self, message: CouncilMessage) -> CouncilMessage:
        """Commit one message.  Its author is whatever the runtime passed in;
        nothing here reads the content to decide who wrote it (§3.2)."""
        parsed = self._as(CouncilMessage, message, "message")
        payload = parsed.to_dict()
        payload["created_at"] = payload.get("created_at") or now_iso()
        stored = self._as(CouncilMessage, payload, "message")
        try:
            with self._db() as conn:
                self._insert(conn, "council_messages", _MESSAGE_COLUMNS,
                             stored.to_dict(), label="message", row_id=stored.id)
        except sqlite3.Error as exc:
            raise CouncilStoreError("message", f"could not store {stored.id}: {exc}") from exc
        return stored

    def list_messages(self, session_id: str, *, viewer_id: str = "",
                      since_id: str = "", limit: int = 200,
                      audit: bool = False) -> List[CouncilMessage]:
        """The room as one reader may see it.

        Half of the blind-round guarantee lives here: a message with
        `visibility="participant"` addressed to `p_a` is not in `p_b`'s list.
        The other half is that the filter is *this* function, so a second route
        cannot forget it.

        Three readers get everything: `audit=True` (the explicit audit path),
        the session's owner, and each message's own author.  An empty
        `viewer_id` without `audit` is nobody — it sees only what the whole room
        can see, so a forgotten parameter under-shares instead of leaking.

        `limit` bounds the rows *read*, not the rows returned: a viewer whose
        window is mostly other people's private traffic gets fewer than `limit`
        messages back.  Filtering after the limit is the only order that cannot
        leak — counting visible rows first would mean reading them first.

        `since_id` is a cursor: messages after that one, by insertion order.  An
        unknown `since_id` starts from the beginning and says so in the log —
        for a transcript, showing a message twice is a smaller failure than
        silently dropping the ones a reconnecting client had not seen."""
        room = str(session_id or "")
        viewer = str(viewer_id or "").strip()
        capped = self._limit(limit, 200, ceiling=2000)
        since_seq = 0
        try:
            with self._db() as conn:
                owner_row = conn.execute(
                    "SELECT owner FROM council_sessions WHERE id = ?", (room,)).fetchone()
                if since_id:
                    cursor_row = conn.execute(
                        "SELECT seq FROM council_messages WHERE id = ?",
                        (str(since_id),)).fetchone()
                    if cursor_row is None:
                        logger.warning(
                            "council: since_id %s is not a message in %s; "
                            "listing from the start", since_id, room)
                    else:
                        since_seq = int(cursor_row["seq"])
                rows = conn.execute(
                    "SELECT * FROM council_messages WHERE session_id = ? AND seq > ? "
                    "ORDER BY seq LIMIT ?", (room, since_seq, capped)).fetchall()
        except (sqlite3.Error, CouncilStoreError) as exc:
            logger.warning("council: list_messages(%s) failed: %s", room, exc)
            return []
        session_owner = str(owner_row["owner"]) if owner_row is not None else ""
        is_owner = bool(audit) or bool(viewer and session_owner and viewer == session_owner)
        out: List[CouncilMessage] = []
        for row in rows:
            message = _parse_row(row, CouncilMessage, json_cols=_MESSAGE_JSON, drop=("seq",))
            if message is None:
                continue
            if is_owner:
                out.append(message)
            elif not viewer:
                # Nobody in particular: the room's public transcript, and not a
                # line more.  Under-sharing is the safe direction for a caller
                # that forgot to say who is reading.
                if message.visibility == "room":
                    out.append(message)
            elif message.is_visible_to(viewer):
                out.append(message)
        return out

    def get_message(self, message_id: str) -> Optional[CouncilMessage]:
        return self._fetch_one("SELECT * FROM council_messages WHERE id = ?",
                               [str(message_id or "")], CouncilMessage,
                               json_cols=_MESSAGE_JSON, drop=("seq",))


    # ── turns ──────────────────────────────────────────────────────────────

    def create_turn(self, turn: CouncilTurn) -> CouncilTurn:
        """Open a turn.

        A turn carrying an `idempotency_key` already used in this room is
        refused with `DuplicateTurn` naming the turn that has it.  The unique
        index does the refusing, so the guarantee holds between two processes
        and not merely between two threads (§15.3)."""
        parsed = self._as(CouncilTurn, turn, "turn")
        stamp = now_iso()
        payload = parsed.to_dict()
        payload["created_at"] = payload.get("created_at") or stamp
        payload["updated_at"] = payload.get("updated_at") or stamp
        stored = self._as(CouncilTurn, payload, "turn")
        try:
            with self._db() as conn:
                try:
                    conn.execute(_insert_sql("council_turns", _TURN_COLUMNS),
                                 _values(stored.to_dict(), _TURN_COLUMNS))
                except sqlite3.IntegrityError as exc:
                    existing = ""
                    if stored.idempotency_key:
                        row = conn.execute(
                            "SELECT id FROM council_turns WHERE session_id = ? "
                            "AND idempotency_key = ?",
                            (stored.session_id, stored.idempotency_key)).fetchone()
                        existing = str(row["id"]) if row else ""
                    if existing:
                        raise DuplicateTurn(stored.idempotency_key, existing) from exc
                    raise CouncilStoreError(
                        "turn", f"{stored.id!r} already exists ({exc})") from exc
        except sqlite3.Error as exc:
            raise CouncilStoreError("turn", f"could not store {stored.id}: {exc}") from exc
        return stored

    def get_turn(self, turn_id: str) -> Optional[CouncilTurn]:
        return self._fetch_one("SELECT * FROM council_turns WHERE id = ?",
                               [str(turn_id or "")], CouncilTurn, json_cols=_TURN_JSON)

    def list_turns(self, session_id: str, *, state: str = "",
                   limit: int = 100) -> List[CouncilTurn]:
        sql = "SELECT * FROM council_turns WHERE session_id = ?"
        params: List[Any] = [str(session_id or "")]
        if state:
            sql += " AND state = ?"
            params.append(str(state))
        sql += " ORDER BY created_at, id LIMIT ?"
        return self._fetch_all(sql, params + [self._limit(limit, 100)],
                               CouncilTurn, json_cols=_TURN_JSON)

    def update_turn(self, turn_id: str, patch: Mapping[str, Any]) -> CouncilTurn:
        """Move a turn along its declared graph.

        The transition is checked before the row is written, and the row is
        written before anyone emits an event (§8) — an event for a transition
        that then failed to persist is how a consumer ends up ahead of the
        source of truth.  Writing the same state twice is allowed and is a
        no-op: a retry that re-asserts where it already is should not have to
        find a self-edge in the graph."""
        with self._db() as conn:
            row = conn.execute("SELECT * FROM council_turns WHERE id = ?",
                               (str(turn_id or ""),)).fetchone()
            if row is None:
                raise NotFound("turn", turn_id)
            current = _parse_row(row, CouncilTurn, json_cols=_TURN_JSON)
            if current is None:
                raise CouncilStoreError("turn", f"turn {turn_id} is stored corrupt")
            merged = _merged(current.to_dict(), patch, _TURN_MUTABLE, label="turn")
            wanted = str(merged.get("state") or current.state)
            if wanted != current.state:
                check_transition(current.state, wanted, graph=TRANSITIONS)
            merged["updated_at"] = now_iso()
            updated = self._as(CouncilTurn, merged, "turn")
            conn.execute(_update_sql("council_turns", _TURN_COLUMNS),
                         _values(updated.to_dict(),
                                 [c for c in _TURN_COLUMNS if c != "id"]) + (updated.id,))
        return updated

    # ── tasks ──────────────────────────────────────────────────────────────

    def create_task(self, task: CouncilTask) -> CouncilTask:
        parsed = self._as(CouncilTask, task, "task")
        stamp = now_iso()
        payload = parsed.to_dict()
        payload["created_at"] = payload.get("created_at") or stamp
        payload["updated_at"] = payload.get("updated_at") or stamp
        stored = self._as(CouncilTask, payload, "task")
        try:
            with self._db() as conn:
                self._insert(conn, "council_tasks", _TASK_COLUMNS, stored.to_dict(),
                             label="task", row_id=stored.id)
        except sqlite3.Error as exc:
            raise CouncilStoreError("task", f"could not store {stored.id}: {exc}") from exc
        return stored

    def get_task(self, task_id: str) -> Optional[CouncilTask]:
        return self._fetch_one("SELECT * FROM council_tasks WHERE id = ?",
                               [str(task_id or "")], CouncilTask, json_cols=_TASK_JSON)

    def list_tasks(self, session_id: str, *, status: str = "",
                   owner_participant_id: str = "", limit: int = 200) -> List[CouncilTask]:
        sql = "SELECT * FROM council_tasks WHERE session_id = ?"
        params: List[Any] = [str(session_id or "")]
        if status:
            sql += " AND status = ?"
            params.append(str(status))
        if owner_participant_id:
            sql += " AND owner_participant_id = ?"
            params.append(str(owner_participant_id))
        sql += " ORDER BY created_at, id LIMIT ?"
        return self._fetch_all(sql, params + [self._limit(limit, 200)],
                               CouncilTask, json_cols=_TASK_JSON)

    def update_task(self, task_id: str, patch: Mapping[str, Any]) -> CouncilTask:
        with self._db() as conn:
            row = conn.execute("SELECT * FROM council_tasks WHERE id = ?",
                               (str(task_id or ""),)).fetchone()
            if row is None:
                raise NotFound("task", task_id)
            current = _parse_row(row, CouncilTask, json_cols=_TASK_JSON)
            if current is None:
                raise CouncilStoreError("task", f"task {task_id} is stored corrupt")
            merged = _merged(current.to_dict(), patch, _TASK_MUTABLE, label="task")
            merged["updated_at"] = now_iso()
            updated = self._as(CouncilTask, merged, "task")
            conn.execute(_update_sql("council_tasks", _TASK_COLUMNS),
                         _values(updated.to_dict(),
                                 [c for c in _TASK_COLUMNS if c != "id"]) + (updated.id,))
        return updated


    # ── claims ─────────────────────────────────────────────────────────────

    def create_claim(self, claim: CouncilClaim) -> CouncilClaim:
        """Record a claim on a resource, or refuse because somebody holds it.

        The refusal comes from a unique index over the active states, not from
        a read-then-write in this method: two drivers asking at the same instant
        must not both be told yes, and a check followed by an insert is exactly
        the race that lets them (§3.3, §20)."""
        parsed = self._as(CouncilClaim, claim, "claim")
        payload = parsed.to_dict()
        if parsed.state == "held" and not payload.get("acquired_at"):
            payload["acquired_at"] = now_iso()
        stored = self._as(CouncilClaim, payload, "claim")
        try:
            with self._db() as conn:
                try:
                    conn.execute(_insert_sql("council_claims", _CLAIM_COLUMNS),
                                 _values(stored.to_dict(), _CLAIM_COLUMNS))
                except sqlite3.IntegrityError as exc:
                    self._raise_claim_conflict(conn, stored, exc)
        except sqlite3.Error as exc:
            raise CouncilStoreError("claim", f"could not store {stored.id}: {exc}") from exc
        return stored

    @staticmethod
    def _raise_claim_conflict(conn: sqlite3.Connection, claim: CouncilClaim,
                              exc: sqlite3.IntegrityError) -> None:
        """Turn an index violation into an answer a driver can act on."""
        row = conn.execute(
            "SELECT id, holder_id FROM council_claims WHERE session_id = ? AND kind = ? "
            "AND resource = ? AND state IN (" + ", ".join("?" for _ in ACTIVE_CLAIM_STATES)
            + ") AND id <> ?",
            (claim.session_id, claim.kind, claim.resource, *ACTIVE_CLAIM_STATES, claim.id),
        ).fetchone()
        if row is not None:
            raise ClaimConflict(claim.resource, str(row["holder_id"]), str(row["id"])) from exc
        raise CouncilStoreError("claim", f"{claim.id!r} already exists ({exc})") from exc

    def get_claim(self, claim_id: str) -> Optional[CouncilClaim]:
        return self._fetch_one("SELECT * FROM council_claims WHERE id = ?",
                               [str(claim_id or "")], CouncilClaim, json_cols=_CLAIM_JSON)

    def list_claims(self, session_id: str, *, state: str = "", holder_id: str = "",
                    resource: str = "", limit: int = 200) -> List[CouncilClaim]:
        sql = "SELECT * FROM council_claims WHERE session_id = ?"
        params: List[Any] = [str(session_id or "")]
        if state:
            sql += " AND state = ?"
            params.append(str(state))
        if holder_id:
            sql += " AND holder_id = ?"
            params.append(str(holder_id))
        if resource:
            sql += " AND resource = ?"
            params.append(str(resource))
        sql += " ORDER BY acquired_at, id LIMIT ?"
        return self._fetch_all(sql, params + [self._limit(limit, 200)],
                               CouncilClaim, json_cols=_CLAIM_JSON)

    def update_claim(self, claim_id: str, patch: Mapping[str, Any]) -> CouncilClaim:
        """Change a claim's state, holder or note.

        `kind` and `resource` are not updatable: a handoff moves the holder of
        *this* resource, and letting a claim be repointed would make the audit
        trail say the new owner had always held the new file (§11.3)."""
        with self._db() as conn:
            row = conn.execute("SELECT * FROM council_claims WHERE id = ?",
                               (str(claim_id or ""),)).fetchone()
            if row is None:
                raise NotFound("claim", claim_id)
            current = _parse_row(row, CouncilClaim, json_cols=_CLAIM_JSON)
            if current is None:
                raise CouncilStoreError("claim", f"claim {claim_id} is stored corrupt")
            merged = _merged(current.to_dict(), patch, _CLAIM_MUTABLE, label="claim")
            updated = self._as(CouncilClaim, merged, "claim")
            try:
                conn.execute(_update_sql("council_claims", _CLAIM_COLUMNS),
                             _values(updated.to_dict(),
                                     [c for c in _CLAIM_COLUMNS if c != "id"]) + (updated.id,))
            except sqlite3.IntegrityError as exc:
                self._raise_claim_conflict(conn, updated, exc)
        return updated

    # ── objections ─────────────────────────────────────────────────────────

    def create_objection(self, objection: CouncilObjection) -> CouncilObjection:
        parsed = self._as(CouncilObjection, objection, "objection")
        payload = parsed.to_dict()
        payload["created_at"] = payload.get("created_at") or now_iso()
        stored = self._as(CouncilObjection, payload, "objection")
        try:
            with self._db() as conn:
                self._insert(conn, "council_objections", _OBJECTION_COLUMNS,
                             stored.to_dict(), label="objection", row_id=stored.id)
        except sqlite3.Error as exc:
            raise CouncilStoreError(
                "objection", f"could not store {stored.id}: {exc}") from exc
        return stored

    def get_objection(self, objection_id: str) -> Optional[CouncilObjection]:
        return self._fetch_one("SELECT * FROM council_objections WHERE id = ?",
                               [str(objection_id or "")], CouncilObjection,
                               json_cols=_OBJECTION_JSON)

    def list_objections(self, session_id: str, *, status: str = "", severity: str = "",
                        target_id: str = "", limit: int = 200) -> List[CouncilObjection]:
        sql = "SELECT * FROM council_objections WHERE session_id = ?"
        params: List[Any] = [str(session_id or "")]
        if status:
            sql += " AND status = ?"
            params.append(str(status))
        if severity:
            sql += " AND severity = ?"
            params.append(str(severity))
        if target_id:
            sql += " AND target_id = ?"
            params.append(str(target_id))
        sql += " ORDER BY created_at, id LIMIT ?"
        return self._fetch_all(sql, params + [self._limit(limit, 200)],
                               CouncilObjection, json_cols=_OBJECTION_JSON)

    def update_objection(self, objection_id: str,
                         patch: Mapping[str, Any]) -> CouncilObjection:
        """Answer an objection: a status, a note, more evidence.

        What was objected to cannot change.  An objection whose `claim` can be
        rewritten after it is resolved is an objection whose resolution stops
        being about anything (§12.1)."""
        with self._db() as conn:
            row = conn.execute("SELECT * FROM council_objections WHERE id = ?",
                               (str(objection_id or ""),)).fetchone()
            if row is None:
                raise NotFound("objection", objection_id)
            current = _parse_row(row, CouncilObjection, json_cols=_OBJECTION_JSON)
            if current is None:
                raise CouncilStoreError(
                    "objection", f"objection {objection_id} is stored corrupt")
            merged = _merged(current.to_dict(), patch, _OBJECTION_MUTABLE, label="objection")
            if merged.get("status") != current.status and merged["status"] not in ("open",):
                merged["resolved_at"] = merged.get("resolved_at") or now_iso()
            updated = self._as(CouncilObjection, merged, "objection")
            conn.execute(_update_sql("council_objections", _OBJECTION_COLUMNS),
                         _values(updated.to_dict(),
                                 [c for c in _OBJECTION_COLUMNS if c != "id"])
                         + (updated.id,))
        return updated


    # ── decisions (content is immutable) ───────────────────────────────────
    #
    # There is deliberately no `update_decision`.  §12: never quietly edit an
    # earlier decision — create another one with `supersedes`.  The only thing
    # that moves is the status, along the graph in `DECISION_TRANSITIONS`, and
    # the only way to reach `superseded` is `supersede_decision`, which writes
    # the replacement in the same transaction.  A decision whose `chosen` or
    # `dissenters` could be rewritten is a room whose disagreements can be
    # deleted after the fact, and those are the two most expensive fields here.

    def create_decision(self, decision: CouncilDecision) -> CouncilDecision:
        parsed = self._as(CouncilDecision, decision, "decision")
        payload = parsed.to_dict()
        payload["created_at"] = payload.get("created_at") or now_iso()
        stored = self._as(CouncilDecision, payload, "decision")
        try:
            with self._db() as conn:
                self._insert(conn, "council_decisions", _DECISION_COLUMNS,
                             stored.to_dict(), label="decision", row_id=stored.id)
        except sqlite3.Error as exc:
            raise CouncilStoreError(
                "decision", f"could not store {stored.id}: {exc}") from exc
        return stored

    def get_decision(self, decision_id: str) -> Optional[CouncilDecision]:
        return self._fetch_one("SELECT * FROM council_decisions WHERE id = ?",
                               [str(decision_id or "")], CouncilDecision,
                               json_cols=_DECISION_JSON)

    def list_decisions(self, session_id: str, *, status: str = "",
                       limit: int = 200) -> List[CouncilDecision]:
        sql = "SELECT * FROM council_decisions WHERE session_id = ?"
        params: List[Any] = [str(session_id or "")]
        if status:
            sql += " AND status = ?"
            params.append(str(status))
        sql += " ORDER BY created_at, id LIMIT ?"
        return self._fetch_all(sql, params + [self._limit(limit, 200)],
                               CouncilDecision, json_cols=_DECISION_JSON)

    def set_decision_status(self, decision_id: str, status: str) -> CouncilDecision:
        """Move a decision along its lifecycle: proposed → decided → withdrawn.

        `superseded` is not reachable from here on purpose — reaching it without
        writing the replacement in the same breath would leave a room with a
        retired decision and nothing in its place."""
        wanted = str(status or "").strip()
        if wanted == "superseded":
            raise CouncilStoreError(
                "decision.status",
                "use supersede_decision(); a decision is retired by the one that "
                "replaces it, in the same transaction")
        with self._db() as conn:
            current = self._decision_for_update(conn, decision_id)
            check_transition(current.status, wanted, graph=DECISION_TRANSITIONS)
            updated = self._as(CouncilDecision,
                               {**current.to_dict(), "status": wanted}, "decision")
            conn.execute("UPDATE council_decisions SET status = ? WHERE id = ?",
                         (updated.status, updated.id))
        return updated

    def supersede_decision(self, previous_id: str, replacement: CouncilDecision
                           ) -> Tuple[CouncilDecision, CouncilDecision]:
        """Retire a decision and record the one that replaces it, atomically.

        Both rows survive.  `contracts.supersede()` does the checking, so the
        rule lives in one place and a caller that never touches this store gets
        the same answer."""
        parsed = self._as(CouncilDecision, replacement, "decision")
        with self._db() as conn:
            previous = self._decision_for_update(conn, previous_id)
            payload = parsed.to_dict()
            payload["created_at"] = payload.get("created_at") or now_iso()
            payload["session_id"] = payload.get("session_id") or previous.session_id
            retired, current = supersede(previous, self._as(CouncilDecision, payload, "decision"))
            conn.execute("UPDATE council_decisions SET status = ? WHERE id = ?",
                         (retired.status, retired.id))
            self._insert(conn, "council_decisions", _DECISION_COLUMNS,
                         current.to_dict(), label="decision", row_id=current.id)
        return retired, current

    def _decision_for_update(self, conn: sqlite3.Connection,
                             decision_id: str) -> CouncilDecision:
        row = conn.execute("SELECT * FROM council_decisions WHERE id = ?",
                           (str(decision_id or ""),)).fetchone()
        if row is None:
            raise NotFound("decision", decision_id)
        current = _parse_row(row, CouncilDecision, json_cols=_DECISION_JSON)
        if current is None:
            raise CouncilStoreError("decision", f"decision {decision_id} is stored corrupt")
        return current

    # ── idempotency ────────────────────────────────────────────────────────

    def claim_idempotency(self, session_id: str, key: str, *, kind: str,
                          ref: str = "") -> Tuple[bool, str]:
        """Claim a key once.  `(True, ref)` the first time, `(False, the ref
        that won)` every time after (§15.3, §20).

        This is the mechanism, not a convenience: a repeated POST, a client that
        lost its response and a worker that restarted all arrive with the same
        key, and each of them must be answered with the *first* outcome rather
        than given a second turn, a second worker and a second bill.

        `ref` is what the caller wants to remember — usually the id it is about
        to create.  Left empty, one is minted from `kind`, so a caller can claim
        first and name the thing afterwards."""
        room = str(session_id or "")
        token = str(key or "").strip()
        if not token:
            raise CouncilStoreError(
                "idempotency.key",
                "a blank key cannot deduplicate anything; pass the client's key "
                "or do not claim")
        minted = str(ref or "") or new_id(str(kind or "ref"))
        with self._db() as conn:
            row = conn.execute(
                "SELECT ref FROM council_idempotency WHERE session_id = ? AND key = ?",
                (room, token)).fetchone()
            if row is not None:
                return False, str(row["ref"])
            try:
                conn.execute(
                    "INSERT INTO council_idempotency (session_id, key, kind, ref, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (room, token, str(kind or ""), minted, now_iso()))
            except sqlite3.IntegrityError:
                # Another process won between the SELECT and the INSERT.  Its
                # ref is the answer; ours never existed.
                row = conn.execute(
                    "SELECT ref FROM council_idempotency WHERE session_id = ? AND key = ?",
                    (room, token)).fetchone()
                return False, (str(row["ref"]) if row is not None else minted)
        return True, minted

    def idempotency_ref(self, session_id: str, key: str) -> str:
        """What a key already resolved to, or `""`.  A read; never raises."""
        row_ref = ""
        try:
            with self._db() as conn:
                row = conn.execute(
                    "SELECT ref FROM council_idempotency WHERE session_id = ? AND key = ?",
                    (str(session_id or ""), str(key or "").strip())).fetchone()
                row_ref = str(row["ref"]) if row is not None else ""
        except (sqlite3.Error, CouncilStoreError) as exc:
            logger.warning("council: idempotency_ref failed: %s", exc)
        return row_ref


    # ── recovery ───────────────────────────────────────────────────────────

    def recover(self, *, now: Any = None) -> Dict[str, Any]:
        """What runs at start-up, before any executor exists (§15.2).

        Three writes and one report, and the order of the two matters:

        * a room that was `active`, `completing` or `cancelling` becomes
          `interrupted`, and its revision is bumped so that a write still in
          flight from the process that died cannot land on top of the recovery;
        * a turn in any state that is not at rest becomes `interrupted` — its
          `stop_reason` is left alone, because "the process died" is not a
          reason the room reached a conclusion;
        * every claim still in an active state is released.

        That last one deserves its reason.  §15.2 says orphan claims are not
        released until it is established that no live execution holds them —
        and this method is the one moment when that is knowable for free: it
        runs before anything is started, so nothing is alive, so no live
        execution holds anything.  Called at any other time it would be wrong,
        which is why it is called exactly once, from start-up.

        Nothing is retried and no effect is repeated.  Tasks that were mid-flight
        are *reported* rather than touched: reconciling them belongs to whoever
        owns their `run_id` in Dispatch/Subagents, and this store guessing at
        their outcome is how a restart invents a result nobody produced.

        Running it twice is safe: the second pass finds nothing and says so."""
        stamp = _stamp(now)
        sessions: List[str] = []
        turns: List[str] = []
        claims: List[str] = []
        tasks: List[str] = []
        rest = tuple(TURN_STATES_AT_REST)
        with self._db() as conn:
            for row in conn.execute(
                    "SELECT id, status FROM council_sessions "
                    "WHERE status IN ('active', 'completing', 'cancelling')").fetchall():
                try:
                    check_transition(str(row["status"]), "interrupted",
                                     graph=SESSION_TRANSITIONS)
                except CouncilError as exc:  # pragma: no cover - graph says otherwise
                    logger.error("council.recover: %s left at %s (%s)",
                                 row["id"], row["status"], exc)
                    continue
                conn.execute(
                    "UPDATE council_sessions SET status = 'interrupted', "
                    "revision = revision + 1, updated_at = ? WHERE id = ?",
                    (stamp, row["id"]))
                sessions.append(str(row["id"]))

            placeholders = ", ".join("?" for _ in rest)
            for row in conn.execute(
                    f"SELECT id, state FROM council_turns WHERE state NOT IN ({placeholders})",
                    rest).fetchall():
                try:
                    check_transition(str(row["state"]), TURN_INTERRUPTED, graph=TRANSITIONS)
                except CouncilError as exc:  # pragma: no cover - graph says otherwise
                    logger.error("council.recover: turn %s left at %s (%s)",
                                 row["id"], row["state"], exc)
                    continue
                conn.execute(
                    "UPDATE council_turns SET state = ?, updated_at = ? WHERE id = ?",
                    (TURN_INTERRUPTED, stamp, row["id"]))
                turns.append(str(row["id"]))

            active = ", ".join("?" for _ in ACTIVE_CLAIM_STATES)
            for row in conn.execute(
                    f"SELECT id, note FROM council_claims WHERE state IN ({active})",
                    ACTIVE_CLAIM_STATES).fetchall():
                note = (str(row["note"] or "")
                        + (" | " if row["note"] else "")
                        + f"released by recovery at {stamp}: the process holding it is gone")
                conn.execute("UPDATE council_claims SET state = 'released', note = ? "
                             "WHERE id = ?", (note[:1024], row["id"]))
                claims.append(str(row["id"]))

            tasks = [str(r["id"]) for r in conn.execute(
                "SELECT id FROM council_tasks WHERE status IN "
                "('claimed', 'running', 'review')").fetchall()]

        report = {
            "at": stamp,
            "sessions_interrupted": sessions,
            "turns_interrupted": turns,
            "claims_released": claims,
            "tasks_needing_reconciliation": tasks,
            "effects_repeated": [],
            "counts": {
                "sessions_interrupted": len(sessions),
                "turns_interrupted": len(turns),
                "claims_released": len(claims),
                "tasks_needing_reconciliation": len(tasks),
            },
        }
        if any(report["counts"].values()):
            logger.info("council.recover: %s", report["counts"])
        return report

    # ── diagnostics ────────────────────────────────────────────────────────

    def stats(self) -> Dict[str, Any]:
        """How big is each table, right now, and where does it live.

        A read: it never raises.  A table that cannot be counted reports -1
        rather than vanishing from the report — a missing line reads as "no
        rows", and those are very different answers to have about a ledger."""
        out: Dict[str, Any] = {
            "path": self.path,
            "schemas": list(registered_schemas()),
            "bytes": 0,
            "tables": {},
        }
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(OSError):
                out["bytes"] += os.path.getsize(self.path + suffix)
        try:
            with self._db() as conn:
                names = [r["name"] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%'")]
                for name in names:
                    try:
                        out["tables"][name] = int(conn.execute(
                            f'SELECT COUNT(*) AS n FROM "{name}"').fetchone()["n"])
                    except sqlite3.Error:
                        out["tables"][name] = -1
        except (sqlite3.Error, CouncilStoreError) as exc:
            logger.warning("council: stats failed: %s", exc)
        return out
