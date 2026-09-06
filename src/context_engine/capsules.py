"""
context_engine/capsules.py — the small thing you can still read after the
window is gone.

Every long task in this app eventually crosses a boundary the model does not
survive: a compaction, a model swap mid-run, a delegation to a worker, a
restart after the machine slept.  What happened before that boundary is
usually recoverable from the transcript, and recovering it is exactly what
nobody has the tokens to do.  So the agent re-reads the same files, re-asks the
same question, and — the expensive one — **re-runs an action whose result it
never confirmed**, because "I ran the migration and did not see the output" and
"I did not run the migration" look identical from the far side of a compaction.

A capsule is the checkpoint that survives: objective, definition of done,
phase, what is done, what is next, what was decided, what is still open, who
holds what, what the evidence is, and how sure we are that the last effect
actually landed.  Small enough to always fit; structured enough to be checked.

Four rules hold the whole thing up:

* **Deltas, never free rewriting.**  A model handed the capsule back as prose
  would silently drop the two decisions it did not consider important.  So the
  only way in is a typed op from `CAPSULE_DELTA_OPS`, applied by this module,
  and a bad delta is rejected by itself — one malformed op does not take the
  other nine down with it.  Same shape as `services.objectives.apply_deltas`.

* **Append-only beside it.**  Every applied batch writes a row to `capsule_log`
  with the actor, the timestamp and the deltas.  "Why does the capsule say the
  worker owns that file?" has to have an answer that is not "someone wrote it".

* **Idempotent by text.**  Compacting twice must not produce two copies of the
  same decision.  `add_next_action`, `record_decision`, `complete`,
  `open_question` and `add_evidence` compare on whitespace-collapsed, case
  folded text, so the second identical delta is applied to a no-op instead of
  appending a near-duplicate the next reader has to reconcile.

* **An uncertain effect is not repeated.**  When `last_verified_state` is
  `partial` or `unknown`, `render()` says so in a line of its own, in the
  prompt, above everything else the capsule claims.  That line is the entire
  reason this file exists.

`validate()` marks broken references and deletes nothing.  A claim on a file
that has since been renamed is information; silently dropping it turns a
question the human could answer into one nobody knows to ask.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from src.contracts.base import (
    ContractError,
    as_mapping,
    one_of,
    reject_unknown,
    text,
    text_list,
    timestamp,
    whole,
)

from . import store
from .contracts import ContextCandidate

logger = logging.getLogger(__name__)

# ── vocabulary ─────────────────────────────────────────────────────────────

CAPSULE_DELTA_OPS: Tuple[str, ...] = (
    "set_phase", "complete", "add_next_action", "drop_next_action",
    "record_decision", "open_question", "close_question",
    "claim", "release", "add_evidence", "set_state", "set_verdict",
)

#: How sure we are that the last effect landed.  `partial` and `unknown` are
#: the two that make `render()` shout; `failed` is a known outcome and needs no
#: warning, because a failure the agent knows about is not an uncertain effect.
VERIFIED_STATES: Tuple[str, ...] = ("verified", "partial", "unknown", "failed")
UNCERTAIN_STATES: Tuple[str, ...] = ("partial", "unknown")

#: Ops whose payload is appended to a list, deduplicated on normalised text.
APPEND_OPS: Dict[str, str] = {
    "complete": "completed",
    "add_next_action": "next_actions",
    "record_decision": "decisions",
    "open_question": "open_questions",
    "add_evidence": "evidence",
}
#: Ops that remove an entry from one of those lists.
REMOVE_OPS: Dict[str, str] = {
    "drop_next_action": "next_actions",
    "close_question": "open_questions",
}

#: Keys a single delta may carry.  Rule 2 of `contracts/base`: an unknown key
#: is an error, never a default — `{"op": "claim", "onwer": "w1"}` must not
#: quietly become a claim held by nobody.
DELTA_KEYS: Tuple[str, ...] = ("op", "value", "holder", "source_revision", "rationale")

MAX_ENTRY_CHARS = 512
MAX_LIST_ITEMS = 200
MAX_CLAIMS = 200
MAX_TEXT_CHARS = 4000

#: The exact line `render()` emits when the last effect is not confirmed.
#: Exported so a test — and a prompt reviewer — can assert on the contract
#: rather than on prose that will get reworded.
UNCERTAIN_EFFECT_NOTE = (
    "# UNCERTAIN EFFECT: the last action's result was never confirmed. "
    "Verify the current state before repeating it; do not re-run it blind."
)


class CapsuleError(ValueError):
    """Invalid capsule input or an unusable store.  Routes map this to a 400."""


class CapsuleConflict(CapsuleError):
    """`expected_revision` did not match.

    Carries `revision`, the revision that was actually stored, so a caller that
    lost a race can reload and re-apply instead of overwriting the batch that
    beat it.  Being a `CapsuleError` it is also a `ValueError`."""

    def __init__(self, scope_id: str, expected: Optional[int], actual: int) -> None:
        self.scope_id = str(scope_id)
        self.expected = expected
        self.revision = int(actual)
        super().__init__(
            f"capsule {self.scope_id} is at revision {self.revision}, not {expected}; "
            "reload it and re-apply your deltas"
        )


# ── schema ─────────────────────────────────────────────────────────────────
#
# `scope_id` is the primary key: a session id, run id, council id or branch id
# is already unique across the install, so one capsule per scope needs no
# composite key.  Owner isolation is a WHERE clause (`store.scope_clause`), not
# a key — that way a lookup with the wrong owner returns nothing instead of
# returning somebody else's capsule under a matching id.

store.register_schema("capsules", (
    """
    CREATE TABLE IF NOT EXISTS capsules (
        scope_id            TEXT PRIMARY KEY,
        owner               TEXT NOT NULL DEFAULT '',
        project_id          TEXT NOT NULL DEFAULT '',
        objective           TEXT NOT NULL DEFAULT '',
        definition_of_done  TEXT NOT NULL DEFAULT '[]',
        phase               TEXT NOT NULL DEFAULT 'plan',
        completed           TEXT NOT NULL DEFAULT '[]',
        current_state       TEXT NOT NULL DEFAULT '',
        next_actions        TEXT NOT NULL DEFAULT '[]',
        decisions           TEXT NOT NULL DEFAULT '[]',
        open_questions      TEXT NOT NULL DEFAULT '[]',
        claims              TEXT NOT NULL DEFAULT '{}',
        evidence            TEXT NOT NULL DEFAULT '[]',
        last_verified_state TEXT NOT NULL DEFAULT 'unknown',
        source_revision     INTEGER NOT NULL DEFAULT 0,
        revision            INTEGER NOT NULL DEFAULT 1,
        updated_at          TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_capsules_owner ON capsules(owner, project_id)",
    "CREATE INDEX IF NOT EXISTS idx_capsules_updated ON capsules(updated_at)",
    """
    CREATE TABLE IF NOT EXISTS capsule_log (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        scope_id   TEXT NOT NULL,
        owner      TEXT NOT NULL DEFAULT '',
        project_id TEXT NOT NULL DEFAULT '',
        actor      TEXT NOT NULL DEFAULT '',
        ts         TEXT NOT NULL DEFAULT '',
        revision   INTEGER NOT NULL DEFAULT 0,
        applied    TEXT NOT NULL DEFAULT '[]',
        rejected   TEXT NOT NULL DEFAULT '[]'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_capsule_log_scope ON capsule_log(scope_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_capsule_log_owner ON capsule_log(owner, project_id)",
))

_COLUMNS: Tuple[str, ...] = (
    "scope_id", "owner", "project_id", "objective", "definition_of_done", "phase",
    "completed", "current_state", "next_actions", "decisions", "open_questions",
    "claims", "evidence", "last_verified_state", "source_revision", "revision",
    "updated_at",
)
_JSON_LISTS: Tuple[str, ...] = (
    "definition_of_done", "completed", "next_actions", "decisions",
    "open_questions", "evidence",
)


def _ts(data: Mapping[str, Any], key: str, path: str) -> str:
    raw = data.get(key, None)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return ""
    return timestamp(data, key, path, default="") or ""


def normalise(value: Any) -> str:
    """The key idempotency compares on: whitespace collapsed, case folded.

    Deliberately not a fuzzy match.  "resolve OBJ-17" and "Resolve  OBJ-17" are
    the same action written twice; "resolve OBJ-17" and "resolve OBJ-18" are
    not, and no amount of similarity scoring should be allowed to think so."""
    return " ".join(str(value or "").split()).casefold()


def _claim_map(data: Mapping[str, Any], key: str, path: str) -> Dict[str, str]:
    """`{path: holder}`.  Both halves are strings; a claim held by `None` is a
    claim nobody can be asked about."""
    raw = data.get(key, None)
    if raw is None:
        return {}
    bag = as_mapping(raw, f"{path}.{key}")
    if len(bag) > MAX_CLAIMS:
        raise ContractError(f"{path}.{key}", f"holds more than {MAX_CLAIMS} claims")
    out: Dict[str, str] = {}
    for name, value in bag.items():
        if not isinstance(value, str):
            raise ContractError(f"{path}.{key}.{name}", "expected a string holder", got=value)
        subject = str(name).strip()
        holder = value.strip()
        if not subject or not holder:
            raise ContractError(f"{path}.{key}.{name}", "a claim needs a subject and a holder")
        out[subject[:MAX_ENTRY_CHARS]] = holder[:MAX_ENTRY_CHARS]
    return out


@dataclass(frozen=True)
class Capsule:
    """§8.  What one worker needs to pick this up cold.

    `source_revision` is not this capsule's version — `revision` is.  It is the
    revision of *the world* the capsule was written against (the objectives
    state, the changeset counter, whatever the caller anchors on), and it is
    the number that lets a resumer ask "what changed since?" instead of
    assuming nothing did."""

    scope_id: str = ""
    owner: str = ""
    project_id: str = ""
    objective: str = ""
    definition_of_done: Tuple[str, ...] = ()
    phase: str = "plan"
    completed: Tuple[str, ...] = ()
    current_state: str = ""
    next_actions: Tuple[str, ...] = ()
    decisions: Tuple[str, ...] = ()
    open_questions: Tuple[str, ...] = ()
    claims: Mapping[str, str] = field(default_factory=dict)
    evidence: Tuple[str, ...] = ()
    last_verified_state: str = "unknown"
    source_revision: int = 0
    revision: int = 1
    updated_at: str = ""
    _KEYS = _COLUMNS

    @classmethod
    def parse(cls, raw: Any, path: str = "capsule") -> "Capsule":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)

        def _list(key: str) -> Tuple[str, ...]:
            # unique=False on read: uniqueness is enforced when a delta is
            # applied, and a hand-edited row with a duplicate should degrade to
            # a duplicate, not to an unreadable capsule.
            return text_list(data, key, path, max_items=MAX_LIST_ITEMS,
                             max_len=MAX_ENTRY_CHARS, unique=False)

        return cls(
            scope_id=text(data, "scope_id", path, max_len=128),
            owner=text(data, "owner", path, required=False, max_len=256),
            project_id=text(data, "project_id", path, required=False, max_len=128),
            objective=text(data, "objective", path, required=False,
                           max_len=MAX_TEXT_CHARS, allow_blank=True),
            definition_of_done=_list("definition_of_done"),
            # `phase` is open text, not a closed list: the plan's own example
            # uses "review", which is not one of `contracts.TASK_PHASES`.  The
            # workflow owns its phase names; this only caps their length.
            phase=text(data, "phase", path, required=False, default="plan", max_len=64),
            completed=_list("completed"),
            current_state=text(data, "current_state", path, required=False,
                               max_len=MAX_TEXT_CHARS, allow_blank=True),
            next_actions=_list("next_actions"),
            decisions=_list("decisions"),
            open_questions=_list("open_questions"),
            claims=_claim_map(data, "claims", path),
            evidence=_list("evidence"),
            last_verified_state=one_of(data, "last_verified_state", path,
                                       choices=VERIFIED_STATES, required=False,
                                       default="unknown") or "unknown",
            source_revision=whole(data, "source_revision", path, default=0, minimum=0) or 0,
            revision=whole(data, "revision", path, default=1, minimum=1) or 1,
            updated_at=_ts(data, "updated_at", path),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scope_id": self.scope_id, "owner": self.owner, "project_id": self.project_id,
            "objective": self.objective,
            "definition_of_done": list(self.definition_of_done),
            "phase": self.phase, "completed": list(self.completed),
            "current_state": self.current_state,
            "next_actions": list(self.next_actions),
            "decisions": list(self.decisions),
            "open_questions": list(self.open_questions),
            "claims": dict(self.claims), "evidence": list(self.evidence),
            "last_verified_state": self.last_verified_state,
            "source_revision": self.source_revision, "revision": self.revision,
            "updated_at": self.updated_at,
        }

    def uncertain(self) -> bool:
        """Is the last effect unconfirmed?  The one question a resumer must
        answer before doing anything that writes."""
        return self.last_verified_state in UNCERTAIN_STATES


# ── row <-> capsule ────────────────────────────────────────────────────────

def _to_row(capsule: Capsule) -> Tuple[Any, ...]:
    data = capsule.to_dict()
    return tuple(
        store.dumps(data[col]) if col in _JSON_LISTS or col == "claims" else data[col]
        for col in _COLUMNS
    )


def _from_row(row: Mapping[str, Any]) -> Optional[Capsule]:
    data = dict(row)
    for col in _JSON_LISTS:
        data[col] = [str(v) for v in store.loads_list(data.get(col))]
    data["claims"] = {str(k): str(v) for k, v in store.loads_dict(data.get("claims")).items()}
    try:
        return Capsule.parse(data)
    except ContractError as exc:
        logger.warning("capsules: unusable row %s (%s); treated as absent",
                       data.get("scope_id"), exc)
        return None


_INSERT = ("INSERT INTO capsules (" + ", ".join(_COLUMNS) + ") VALUES ("
           + ", ".join("?" for _ in _COLUMNS) + ")")
_UPDATE = ("UPDATE capsules SET "
           + ", ".join(f"{c} = ?" for c in _COLUMNS if c != "scope_id")
           + " WHERE scope_id = ?")


# ── read ───────────────────────────────────────────────────────────────────

def load(scope_id: str, *, owner: str = "") -> Optional[Capsule]:
    """The capsule for this scope, or None.  Never raises — a resumption path
    that cannot read its capsule starts cold, it does not fail."""
    if not str(scope_id or "").strip():
        return None
    where, params = store.scope_clause(owner)
    try:
        with store.db() as conn:
            row = conn.execute(
                f"SELECT * FROM capsules WHERE scope_id = ? AND {where}",
                (str(scope_id),) + tuple(params)).fetchone()
    except (sqlite3.Error, store.ContextStoreError) as exc:
        logger.warning("capsules: load(%s) failed: %s", scope_id, exc)
        return None
    return _from_row(row) if row else None


def ensure(scope_id: str, *, owner: str = "", project_id: str = "",
           objective: str = "") -> Capsule:
    """Load the capsule, creating an empty one if this scope has none.

    `objective` is only used when creating: `ensure` never overwrites the
    objective of a capsule that already exists, because a delegated worker
    calling `ensure` with its own summary of the task would otherwise rewrite
    the coordinator's."""
    scope = str(scope_id or "").strip()
    if not scope:
        raise CapsuleError("a capsule needs a scope_id (session, run, council or branch)")
    existing = load(scope, owner=owner)
    if existing is not None:
        return existing
    try:
        capsule = Capsule.parse({
            "scope_id": scope, "owner": owner, "project_id": project_id,
            "objective": objective, "phase": "plan", "last_verified_state": "unknown",
            "revision": 1, "updated_at": store.now_iso(),
        })
    except ContractError as exc:
        raise CapsuleError(str(exc)) from exc
    try:
        with store.db() as conn:
            conn.execute(_INSERT.replace("INSERT INTO", "INSERT OR IGNORE INTO"),
                         _to_row(capsule))
    except (sqlite3.Error, store.ContextStoreError) as exc:
        raise CapsuleError(f"could not create capsule {scope}: {exc}") from exc
    return load(scope, owner=owner) or capsule


def log(scope_id: str, *, limit: int = 50) -> List[Dict[str, Any]]:
    """The append-only trail, newest first.

    This is the answer to "why does the capsule say this?", and it outlives the
    capsule on purpose: `delete()` removes the checkpoint, not the record of
    how it got that way."""
    capped = max(1, min(int(limit or 50), 1000))
    try:
        with store.db() as conn:
            rows = store.rows(conn.execute(
                "SELECT * FROM capsule_log WHERE scope_id = ? ORDER BY id DESC LIMIT ?",
                (str(scope_id), capped)))
    except (sqlite3.Error, store.ContextStoreError) as exc:
        logger.warning("capsules: log(%s) failed: %s", scope_id, exc)
        return []
    for row in rows:
        row["applied"] = store.loads_list(row.get("applied"))
        row["rejected"] = store.loads_list(row.get("rejected"))
    return rows


def delete(scope_id: str, *, owner: str = "") -> bool:
    """Drop the capsule.  The log stays: append-only means append-only."""
    where, params = store.scope_clause(owner)
    try:
        with store.db() as conn:
            cur = conn.execute(
                f"DELETE FROM capsules WHERE scope_id = ? AND {where}",
                (str(scope_id),) + tuple(params))
            return bool(cur.rowcount)
    except (sqlite3.Error, store.ContextStoreError) as exc:
        raise CapsuleError(f"could not delete capsule {scope_id}: {exc}") from exc


# ── the delta compiler ─────────────────────────────────────────────────────

def _entry(delta: Mapping[str, Any], key: str = "value",
           limit: int = MAX_ENTRY_CHARS) -> Tuple[str, str]:
    """`(value, reason)` — the reason is empty when the value is usable."""
    raw = delta.get(key, None)
    if not isinstance(raw, str):
        return "", f"{key!r} must be a string"
    value = raw.strip()
    if not value:
        return "", f"{key!r} is required and must not be blank"
    if len(value) > limit:
        return "", f"{key!r} is longer than {limit} characters"
    return value, ""


def _append_unique(items: List[str], value: str) -> bool:
    """Append unless an equivalent entry is already there.  Returns whether the
    list changed — which is how "compacting twice" stays a no-op the second
    time instead of a second copy of the same decision."""
    key = normalise(value)
    if any(normalise(existing) == key for existing in items):
        return False
    if len(items) >= MAX_LIST_ITEMS:
        items.pop(0)
    items.append(value)
    return True


def _remove_matching(items: List[str], value: str) -> bool:
    key = normalise(value)
    kept = [item for item in items if normalise(item) != key]
    if len(kept) == len(items):
        return False
    items[:] = kept
    return True


def _apply_one(state: Dict[str, Any], delta: Mapping[str, Any],
               actor: str) -> Tuple[Optional[Dict[str, Any]], str, bool]:
    """Apply one delta to `state` in place.

    Returns `(applied_entry, reason, changed)`.  A delta that is refused
    returns `(None, reason, False)` and leaves the state exactly as it was —
    that is what lets a batch survive one bad member."""
    op = delta.get("op", None)
    if not isinstance(op, str) or op.strip() not in CAPSULE_DELTA_OPS:
        shown = op if isinstance(op, str) else type(op).__name__
        return None, (f"unknown op {shown!r}; valid ops are {list(CAPSULE_DELTA_OPS)}"), False
    op = op.strip()

    entry: Dict[str, Any] = {"op": op}
    changed = False

    # `source_revision` may ride on any delta: it says which state of the world
    # this batch was written against, and it is validated before the op runs so
    # a bad number cannot half-apply a good delta.
    if "source_revision" in delta:
        raw = delta.get("source_revision")
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            return None, "'source_revision' must be a whole number >= 0", False
        if state["source_revision"] != raw:
            state["source_revision"] = raw
            changed = True
        entry["source_revision"] = raw

    if op in APPEND_OPS:
        value, reason = _entry(delta)
        if reason:
            return None, reason, False
        changed = _append_unique(state[APPEND_OPS[op]], value) or changed
        if op == "complete":
            # Finishing something also takes it off the list of what is next;
            # a resumer that reads both would otherwise do it again.
            changed = _remove_matching(state["next_actions"], value) or changed
        entry["value"] = value

    elif op in REMOVE_OPS:
        value, reason = _entry(delta)
        if reason:
            return None, reason, False
        changed = _remove_matching(state[REMOVE_OPS[op]], value) or changed
        entry["value"] = value

    elif op == "claim":
        subject, reason = _entry(delta)
        if reason:
            return None, reason, False
        holder = str(delta.get("holder") or actor or "").strip()
        if not holder:
            return None, "'claim' needs a holder (or a non-empty actor)", False
        if len(state["claims"]) >= MAX_CLAIMS and subject not in state["claims"]:
            return None, f"more than {MAX_CLAIMS} claims", False
        if state["claims"].get(subject) != holder:
            state["claims"][subject] = holder
            changed = True
        entry["value"] = subject
        entry["holder"] = holder

    elif op == "release":
        subject, reason = _entry(delta)
        if reason:
            return None, reason, False
        if state["claims"].pop(subject, None) is not None:
            changed = True
        entry["value"] = subject

    elif op == "set_phase":
        value, reason = _entry(delta, limit=64)
        if reason:
            return None, reason, False
        if state["phase"] != value:
            state["phase"] = value
            changed = True
        entry["value"] = value

    elif op == "set_state":
        value, reason = _entry(delta, limit=MAX_TEXT_CHARS)
        if reason:
            return None, reason, False
        if state["current_state"] != value:
            state["current_state"] = value
            changed = True
        entry["value"] = value

    elif op == "set_verdict":
        value, reason = _entry(delta, limit=64)
        if reason:
            return None, reason, False
        if value not in VERIFIED_STATES:
            return None, (f"unknown verdict {value!r}; "
                          f"valid verdicts are {list(VERIFIED_STATES)}"), False
        if state["last_verified_state"] != value:
            state["last_verified_state"] = value
            changed = True
        entry["value"] = value

    else:  # pragma: no cover - CAPSULE_DELTA_OPS and the branches above agree
        return None, f"op {op!r} is declared but not implemented", False

    entry["changed"] = changed
    return entry, "", changed


def apply_deltas(scope_id: str, deltas: Sequence[Mapping[str, Any]], *, actor: str,
                 expected_revision: Optional[int] = None,
                 owner: str = "") -> Dict[str, Any]:
    """Apply a batch of typed deltas.  One bad delta does not lose the batch.

    Returns `{"applied": [...], "rejected": [{"delta":…, "reason":…}],
    "capsule": {...}}`.  Raises `CapsuleConflict` when `expected_revision` no
    longer matches, and `CapsuleError` when the capsule does not exist — the
    two cases where continuing would mean writing over something the caller has
    not seen.

    The revision only moves when something actually changed.  A batch that is
    entirely idempotent (the same compaction summary, twice) leaves the
    revision alone, so it cannot invalidate another holder's
    `expected_revision` for no reason — but it is still written to the log,
    because "we tried to record this twice" is itself worth knowing."""
    scope = str(scope_id or "").strip()
    if not scope:
        raise CapsuleError("a capsule needs a scope_id")
    if expected_revision is not None and (isinstance(expected_revision, bool)
                                          or not isinstance(expected_revision, int)):
        raise CapsuleError(
            f"expected_revision must be a whole number, got {expected_revision!r}")
    who = str(actor or "").strip() or "unknown"
    where, params = store.scope_clause(owner)
    applied: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []

    try:
        with store.db() as conn:
            row = conn.execute(
                f"SELECT * FROM capsules WHERE scope_id = ? AND {where}",
                (scope,) + tuple(params)).fetchone()
            if row is None:
                raise CapsuleError(f"capsule {scope} does not exist; call ensure() first")
            current = _from_row(row)
            if current is None:
                raise CapsuleError(f"capsule {scope} is stored corrupt and cannot be updated")
            if expected_revision is not None and int(expected_revision) != current.revision:
                raise CapsuleConflict(scope, expected_revision, current.revision)

            state = current.to_dict()
            changed_any = False
            for delta in deltas or ():
                if not isinstance(delta, Mapping):
                    rejected.append({"delta": delta, "reason": "delta is not an object"})
                    continue
                try:
                    reject_unknown(delta, DELTA_KEYS, "delta")
                except ContractError as exc:
                    rejected.append({"delta": dict(delta), "reason": str(exc)})
                    continue
                entry, reason, changed = _apply_one(state, delta, who)
                if entry is None:
                    rejected.append({"delta": dict(delta), "reason": reason})
                    continue
                applied.append(entry)
                changed_any = changed_any or changed

            if changed_any:
                state["revision"] = current.revision + 1
                state["updated_at"] = store.now_iso()
            try:
                capsule = Capsule.parse(state)
            except ContractError as exc:  # pragma: no cover - a delta built this
                raise CapsuleError(f"deltas produced an invalid capsule: {exc}") from exc
            if changed_any:
                values = _to_row(capsule)
                conn.execute(_UPDATE, tuple(values[1:]) + (capsule.scope_id,))
            if applied or rejected:
                conn.execute(
                    "INSERT INTO capsule_log "
                    "(scope_id, owner, project_id, actor, ts, revision, applied, rejected) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (scope, capsule.owner, capsule.project_id, who, store.now_iso(),
                     capsule.revision, store.dumps(applied), store.dumps(rejected)),
                )
    except (sqlite3.Error, store.ContextStoreError) as exc:
        raise CapsuleError(f"could not update capsule {scope}: {exc}") from exc

    return {"applied": applied, "rejected": rejected, "capsule": capsule.to_dict()}


# ── validation: mark, never delete ─────────────────────────────────────────

def _resolve(workspace: str, ref: str) -> str:
    """A workspace-relative reference as an absolute path, or `""` when it
    escapes the workspace.  A claim on `../../etc/passwd` is not a claim this
    function is going to go and stat."""
    try:
        root = os.path.realpath(workspace)
        target = os.path.realpath(os.path.join(root, ref))
        common = os.path.commonpath([os.path.normcase(root), os.path.normcase(target)])
    except (OSError, ValueError):
        return ""
    return target if common == os.path.normcase(root) else ""


def validate(capsule: Capsule, *, workspace: str = "",
             now: Optional[datetime] = None) -> Dict[str, Any]:
    """Check the capsule's references against the world.  Delete nothing.

    §8 of the plan says to *mark* stale references, and the distinction is the
    whole value: a claim on a file that was renamed is the trace of work
    somebody did.  Dropping it silently converts a question a human can answer
    ("where did that move to?") into a gap nobody knows exists.

    A reference this function cannot resolve — `changeset:9f2c`, `OBJ-17` — is
    reported as `unchecked`, not as missing.  Not owning a namespace is not the
    same as knowing the thing is gone."""
    ws = str(workspace or "").strip()
    stale_claims: List[Dict[str, Any]] = []
    missing_evidence: List[Dict[str, Any]] = []
    unchecked: List[Dict[str, Any]] = []

    for subject, holder in sorted(dict(capsule.claims).items()):
        if not ws:
            unchecked.append({"kind": "claim", "ref": subject,
                              "reason": "no workspace to check against"})
            continue
        target = _resolve(ws, subject)
        if not target:
            stale_claims.append({"subject": subject, "holder": holder,
                                 "reason": "resolves outside the workspace"})
        elif not os.path.exists(target):
            stale_claims.append({"subject": subject, "holder": holder,
                                 "reason": "no longer exists"})

    for ref in capsule.evidence:
        if ref.startswith("file:"):
            rel = ref[len("file:"):].strip()
        elif ":" not in ref:
            rel = ref.strip()          # a bare reference is a workspace path
        else:
            unchecked.append({"kind": "evidence", "ref": ref,
                              "reason": "not a workspace file reference"})
            continue
        if not ws or not rel:
            unchecked.append({"kind": "evidence", "ref": ref,
                              "reason": "no workspace to check against" if not ws
                                        else "empty reference"})
            continue
        target = _resolve(ws, rel)
        if not target:
            missing_evidence.append({"ref": ref, "reason": "resolves outside the workspace"})
        elif not os.path.exists(target):
            missing_evidence.append({"ref": ref, "reason": "no longer exists"})

    return {
        "scope_id": capsule.scope_id,
        "workspace": ws,
        "stale_claims": stale_claims,
        "missing_evidence": missing_evidence,
        "unchecked": unchecked,
        "age_seconds": store.age_seconds(capsule.updated_at, now=now),
        "ok": not stale_claims and not missing_evidence,
    }


# ── rendering ──────────────────────────────────────────────────────────────

def _y(value: Any) -> str:
    """One scalar, double-quoted.  JSON string escaping is a strict subset of
    YAML 1.2's double-quoted style, so this cannot emit something a YAML reader
    will misparse — which matters because a capsule frequently contains a path
    with a backslash in it."""
    return json.dumps(str(value), ensure_ascii=False)


def _emit_list(lines: List[str], name: str, values: Sequence[str]) -> None:
    if not values:
        return
    lines.append(f"{name}:")
    lines.extend(f"  - {_y(v)}" for v in values)


def render(capsule: Capsule) -> str:
    """The compact YAML of §8, ready to drop into a prompt.

    Empty sections are omitted rather than emitted empty: a capsule is read at
    the moment the window is tightest, and eight lines of `decisions: []` is
    eight lines that could have been a decision.

    When `last_verified_state` is `partial` or `unknown` the render carries
    `UNCERTAIN_EFFECT_NOTE` on its own line, directly under the value it is
    about.  That line is the difference between a resumer that checks and a
    resumer that runs the migration a second time."""
    lines: List[str] = ["schema_version: 1", f"scope_id: {_y(capsule.scope_id)}"]
    if capsule.objective:
        lines.append(f"objective: {_y(capsule.objective)}")
    _emit_list(lines, "definition_of_done", capsule.definition_of_done)
    lines.append(f"phase: {_y(capsule.phase)}")
    _emit_list(lines, "completed", capsule.completed)
    if capsule.current_state:
        lines.append(f"current_state: {_y(capsule.current_state)}")
    _emit_list(lines, "next_actions", capsule.next_actions)
    _emit_list(lines, "decisions", capsule.decisions)
    _emit_list(lines, "open_questions", capsule.open_questions)
    if capsule.claims:
        lines.append("claims:")
        lines.extend(f"  {_y(k)}: {_y(v)}" for k, v in sorted(dict(capsule.claims).items()))
    _emit_list(lines, "evidence", capsule.evidence)
    lines.append(f"last_verified_state: {_y(capsule.last_verified_state)}")
    if capsule.uncertain():
        lines.append(UNCERTAIN_EFFECT_NOTE)
    lines.append(f"source_revision: {capsule.source_revision}")
    lines.append(f"revision: {capsule.revision}")
    return "\n".join(lines)


def as_candidate(capsule: Capsule) -> ContextCandidate:
    """The capsule as compiler input.

    The verdict decides the trust class, and it decides it downwards: only a
    `verified` capsule is presented as a proved result.  Everything else is an
    agent's account of what it thinks happened, marked `degraded` so no
    consumer can quote it as an observation of the world."""
    verified = capsule.last_verified_state == "verified"
    return ContextCandidate(
        candidate_id=f"ctxcand_capsule_{capsule.scope_id}"[:128],
        source_type="capsule",
        source_ref=f"capsule:{capsule.scope_id}",
        title=(capsule.objective or f"capsule {capsule.scope_id}")[:512],
        body=render(capsule),
        section="current_state",
        lanes=("mandatory",),
        scores={"recency": 1.0},
        trust_class="proved" if verified else "agent_assertion",
        authority="proved_result" if verified else "agent_claim",
        source_revision=str(capsule.source_revision),
        observed_at=capsule.updated_at,
        owner=capsule.owner,
        project_id=capsule.project_id,
        degraded=not verified,
        meta={"phase": capsule.phase, "revision": capsule.revision,
              "last_verified_state": capsule.last_verified_state,
              "uncertain": capsule.uncertain()},
    )


__all__ = [
    "CAPSULE_DELTA_OPS", "VERIFIED_STATES", "UNCERTAIN_STATES", "DELTA_KEYS",
    "APPEND_OPS", "REMOVE_OPS", "UNCERTAIN_EFFECT_NOTE",
    "MAX_ENTRY_CHARS", "MAX_LIST_ITEMS", "MAX_CLAIMS", "MAX_TEXT_CHARS",
    "CapsuleError", "CapsuleConflict", "Capsule", "normalise",
    "load", "ensure", "apply_deltas", "validate", "as_candidate", "render",
    "delete", "log",
]
