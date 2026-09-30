"""effect_outbox.py — write-ahead record for effects that cannot be repeated.

A chat history is not a transactional record. Once a message has left the
process (an SMTP DATA phase, a POST to a bridge, a webhook delivery) nothing
the caller does afterwards can take it back, and a connection that dies
before the reply arrives leaves the caller unable to tell "it never went"
from "it went and nobody heard". This module is the one place that keeps that
difference honest, independently of the visual replay and of any single run.

The lifecycle, persisted in its own SQLite file::

    prepared -> admitted -> dispatching -> succeeded
                                 |-> failed_before_effect
                                 |-> partial
                                 |-> outcome_unknown -> reconciled
                                 |-> cancelled          (-> reconciled)

* ``prepared``: the intent row is committed (``synchronous=FULL``). Nothing
  has left the process.
* ``admitted``: the row was read back through a fresh connection. Admission
  is the gate: **if the intent cannot be read back, the effect is not sent.**
* ``dispatching``: written before the transport is touched. A process that
  dies here leaves the row here, and start-up recovery turns it into
  ``outcome_unknown`` rather than letting it look like nothing happened.
* ``succeeded`` / ``failed_before_effect`` / ``partial`` / ``outcome_unknown``:
  what the transport could actually tell. Any exception after dispatch began
  is ``outcome_unknown`` unless the transport proved otherwise by raising
  :class:`EffectNotDispatched` (nothing left the process) or
  :class:`EffectRejected` (the destination answered and refused).
* ``reconciled``: only :func:`reconcile` (asking the destination by the
  identifier the effect carried) or :func:`resolve_manually` may leave
  ``outcome_unknown`` or ``partial``. Nothing here ever sends a second time.
* ``cancelled`` keeps its own certainty: cancelled before dispatch is
  certainly no effect; cancelled while dispatching is unknown.

``effect_certainty`` is stored next to the state so that a state name can
never be read as a claim about the world: ``none`` (certainly nothing
happened), ``confirmed``, ``partial``, ``unknown``.

Idempotency is described, never assumed. ``idempotency_scope`` is
``destination_enforced`` only when the caller states the destination itself
deduplicates on the key; otherwise it is ``none`` and the key is merely an
identifier the effect carries (useful for reconciliation). Local
deduplication has a defined scope (``dedup_key``: owner + kind + key) and only
ever refuses to send again; it never marks a retry as idempotent.
"""
from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, Iterator, List, Mapping, Optional, Sequence

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

STATES = ("prepared", "admitted", "dispatching", "succeeded", "failed_before_effect",
          "partial", "outcome_unknown", "reconciled", "cancelled")
CERTAINTIES = ("none", "confirmed", "partial", "unknown")
#: States a row can still be reconciled from.
RECONCILABLE_STATES = ("outcome_unknown", "partial", "cancelled")
#: States in which the same ``dedup_key`` must not be dispatched again.
_DEDUP_BLOCKING_STATES = ("prepared", "admitted", "dispatching", "succeeded", "partial",
                          "outcome_unknown", "reconciled")

_TRANSITIONS: Dict[str, Sequence[str]] = {
    "prepared": ("admitted", "cancelled"),
    "admitted": ("dispatching", "cancelled", "failed_before_effect"),
    "dispatching": ("succeeded", "failed_before_effect", "partial", "outcome_unknown", "cancelled"),
    "partial": ("reconciled",),
    "outcome_unknown": ("reconciled",),
    "cancelled": ("reconciled",),
}

IDEMPOTENCY_SCOPES = ("none", "destination_enforced")
DEDUP_SCOPES = ("none", "owner_kind_key")

#: One id per process start. Recovery only touches rows written by an earlier
#: process, never the in-flight ones of this one.
BOOT_ID = uuid.uuid4().hex


class EffectError(Exception):
    """Base class of this module's own errors."""


class IntentNotPersisted(EffectError):
    """The intent row could not be committed and read back; nothing was sent."""


class InvalidTransition(EffectError):
    pass


class EffectNotDispatched(Exception):
    """Raise from a dispatch callable when nothing left the process (connect
    refused, name not resolved, authentication failed before the payload)."""


class EffectRejected(EffectNotDispatched):
    """The destination answered and explicitly refused; no effect happened."""


class EffectUncertain(Exception):
    """The destination may have acted while the answer was lost."""


class EffectPartial(Exception):
    """Part of the effect landed (some recipients accepted, a multi-part send)."""

    def __init__(self, message: str = "", *, detail: Optional[Mapping[str, Any]] = None):
        super().__init__(message)
        self.detail = dict(detail or {})


class OutcomeUnknownError(RuntimeError):
    """Raised to a caller whose effect may have happened: never read it as
    "not sent". ``effect`` is the outbox record."""

    def __init__(self, message: str, *, effect: Optional[Mapping[str, Any]] = None):
        super().__init__(message)
        self.effect = dict(effect or {})
        self.effect_certainty = "unknown"


class PartialEffectError(RuntimeError):
    def __init__(self, message: str, *, effect: Optional[Mapping[str, Any]] = None):
        super().__init__(message)
        self.effect = dict(effect or {})
        self.effect_certainty = "partial"


# ---------------------------------------------------------------------------
# Context: who is asking (set by the tool layer, read by the transports)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EffectContext:
    owner: str = ""
    session_id: str = ""
    run_id: str = ""
    call_id: str = ""
    attempt_id: str = ""
    tool: str = ""
    #: outbox row opened by the tool layer, joined by the first transport call
    admission_id: str = ""


_CONTEXT: contextvars.ContextVar[Optional[EffectContext]] = contextvars.ContextVar(
    "effect_outbox_context", default=None)


def bind_context(ctx: EffectContext) -> contextvars.Token:
    return _CONTEXT.set(ctx)


def reset_context(token: contextvars.Token) -> None:
    _CONTEXT.reset(token)


def current_context() -> Optional[EffectContext]:
    return _CONTEXT.get()


def new_attempt_id() -> str:
    return "att_" + uuid.uuid4().hex[:24]


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

_DB_PATH_OVERRIDE: Optional[str] = None
_SCHEMA_READY: set = set()
_SCHEMA_LOCK = threading.Lock()


def set_db_path(path: Optional[str]) -> None:
    """Point the outbox at another file (tests, tooling). ``None`` restores
    the default under the data directory."""
    global _DB_PATH_OVERRIDE
    _DB_PATH_OVERRIDE = path


def db_path() -> str:
    if _DB_PATH_OVERRIDE:
        return _DB_PATH_OVERRIDE
    from src.constants import DATA_DIR
    return os.path.join(DATA_DIR, "effect_outbox.sqlite3")


def enabled() -> bool:
    """Setting ``agent_effect_outbox`` (default on). Off restores the
    previous behaviour of the transports; the tool-level admission gate that
    already existed for email intents is unaffected."""
    try:
        from src.settings import get_setting
        return bool(get_setting("agent_effect_outbox", True))
    except Exception:  # noqa: BLE001 - a settings failure must not disable the gate
        return True


_COLUMNS = """
    id TEXT PRIMARY KEY,
    owner TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    run_id TEXT NOT NULL DEFAULT '',
    call_id TEXT NOT NULL DEFAULT '',
    attempt_id TEXT NOT NULL DEFAULT '',
    tool TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,
    destination TEXT NOT NULL DEFAULT '',
    identifier TEXT NOT NULL DEFAULT '',
    identifier_kind TEXT NOT NULL DEFAULT 'none',
    arguments_sha256 TEXT NOT NULL DEFAULT '',
    dedup_key TEXT,
    dedup_scope TEXT NOT NULL DEFAULT 'none',
    idempotency_key TEXT NOT NULL DEFAULT '',
    idempotency_scope TEXT NOT NULL DEFAULT 'none',
    state TEXT NOT NULL,
    effect_certainty TEXT NOT NULL DEFAULT 'none',
    dispatch_count INTEGER NOT NULL DEFAULT 0,
    external_ref TEXT NOT NULL DEFAULT '',
    resolution TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    error_class TEXT NOT NULL DEFAULT '',
    evidence_json TEXT NOT NULL DEFAULT '[]',
    boot_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    admitted_at TEXT,
    dispatch_started_at TEXT,
    settled_at TEXT,
    reconciled_at TEXT,
    schema_version INTEGER NOT NULL DEFAULT 1
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _connect() -> sqlite3.Connection:
    path = db_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    # The intent must be on disk before the effect may start.
    conn.execute("PRAGMA synchronous=FULL")
    if path not in _SCHEMA_READY:
        with _SCHEMA_LOCK:
            conn.execute(f"CREATE TABLE IF NOT EXISTS effect_outbox ({_COLUMNS})")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS effect_outbox_events ("
                "seq INTEGER PRIMARY KEY AUTOINCREMENT, effect_id TEXT NOT NULL, ts TEXT NOT NULL,"
                "from_state TEXT, to_state TEXT NOT NULL, effect_certainty TEXT NOT NULL,"
                "detail_json TEXT NOT NULL DEFAULT '{}')")
            conn.execute("CREATE INDEX IF NOT EXISTS ix_effect_events_eff ON effect_outbox_events(effect_id, seq)")
            conn.execute("CREATE INDEX IF NOT EXISTS ix_effect_outbox_owner_state ON effect_outbox(owner, state)")
            conn.execute("CREATE INDEX IF NOT EXISTS ix_effect_outbox_run ON effect_outbox(run_id, call_id)")
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_effect_outbox_dedup ON effect_outbox(owner, kind, dedup_key) "
                "WHERE dedup_key IS NOT NULL AND state != 'failed_before_effect' "
                "AND NOT (state IN ('reconciled','cancelled') AND effect_certainty = 'none')")
            _SCHEMA_READY.add(path)
    return conn


@contextmanager
def _db() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


def _row(row: sqlite3.Row) -> Dict[str, Any]:
    out = {key: row[key] for key in row.keys()}
    try:
        out["evidence"] = json.loads(out.pop("evidence_json") or "[]")
    except ValueError:
        out["evidence"] = []
    return out


def _clip(text: Any, limit: int) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def arguments_digest(value: Any) -> str:
    raw = value if isinstance(value, (bytes, str)) else json.dumps(value, sort_keys=True, default=str)
    if isinstance(raw, str):
        raw = raw.encode("utf-8", "replace")
    return hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------

def _move(eid: str, to_state: str, *, certainty: str, allowed_from: Sequence[str],
          fields: Optional[Mapping[str, Any]] = None, detail: Optional[Mapping[str, Any]] = None,
          ) -> Dict[str, Any]:
    if to_state not in STATES or certainty not in CERTAINTIES:
        raise ValueError("invalid state or certainty")
    with _db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT * FROM effect_outbox WHERE id = ?", (eid,)).fetchone()
            if row is None:
                raise InvalidTransition(f"unknown effect {eid}")
            current = row["state"]
            if current not in allowed_from or to_state not in _TRANSITIONS.get(current, ()):
                raise InvalidTransition(f"{current} -> {to_state} is not allowed")
            sets = {"state": to_state, "effect_certainty": certainty, **dict(fields or {})}
            clause = ", ".join(f"{key} = ?" for key in sets)
            conn.execute(f"UPDATE effect_outbox SET {clause} WHERE id = ?", (*sets.values(), eid))
            conn.execute(
                "INSERT INTO effect_outbox_events (effect_id, ts, from_state, to_state, effect_certainty, detail_json)"
                " VALUES (?,?,?,?,?,?)",
                (eid, _now(), current, to_state, certainty,
                 json.dumps(dict(detail or {}), ensure_ascii=False, default=str)[:4000]))
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        return _row(conn.execute("SELECT * FROM effect_outbox WHERE id = ?", (eid,)).fetchone())


def prepare(*, kind: str, owner: str = "", destination: str = "", identifier: str = "",
            identifier_kind: str = "none", arguments: Any = None, arguments_sha256: str = "",
            dedup_key: Optional[str] = None, idempotency_key: str = "",
            destination_enforces_idempotency: bool = False,
            context: Optional[EffectContext] = None) -> Dict[str, Any]:
    """Commit the intent. Nothing has left the process when this returns.

    Returns the row, or ``{"deduplicated": True, ...row}`` when the same
    ``dedup_key`` already has a row that must not be dispatched again (a
    previous attempt that succeeded, is in flight, or whose outcome is
    unknown). A failed-before-effect or cancelled row never blocks a retry.
    """
    ctx = context or current_context() or EffectContext()
    eid = "eff_" + uuid.uuid4().hex[:20]
    scope = "destination_enforced" if (idempotency_key and destination_enforces_idempotency) else "none"
    values = (
        eid, owner or ctx.owner, ctx.session_id, ctx.run_id, ctx.call_id, ctx.attempt_id or "",
        ctx.tool, _clip(kind, 80), _clip(destination, 500), _clip(identifier, 300),
        identifier_kind if identifier else "none",
        arguments_sha256 or (arguments_digest(arguments) if arguments is not None else ""),
        dedup_key, "owner_kind_key" if dedup_key else "none", _clip(idempotency_key, 200), scope,
        "prepared", "none", BOOT_ID, _now(),
    )
    try:
        with _db() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT INTO effect_outbox (id, owner, session_id, run_id, call_id, attempt_id, tool, kind,"
                    " destination, identifier, identifier_kind, arguments_sha256, dedup_key, dedup_scope,"
                    " idempotency_key, idempotency_scope, state, effect_certainty, boot_id, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values)
                conn.execute(
                    "INSERT INTO effect_outbox_events (effect_id, ts, from_state, to_state, effect_certainty, detail_json)"
                    " VALUES (?,?,?,?,?,?)", (eid, _now(), None, "prepared", "none", "{}"))
                conn.execute("COMMIT")
            except sqlite3.IntegrityError:
                conn.execute("ROLLBACK")
                existing = conn.execute(
                    "SELECT * FROM effect_outbox WHERE owner = ? AND kind = ? AND dedup_key = ? "
                    "AND state != 'failed_before_effect' "
                    "AND NOT (state IN ('reconciled','cancelled') AND effect_certainty = 'none') ORDER BY created_at DESC LIMIT 1",
                    (owner or ctx.owner, _clip(kind, 80), dedup_key)).fetchone()
                if existing is not None:
                    return {"deduplicated": True, **_row(existing)}
                raise
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
    except sqlite3.Error as exc:
        raise IntentNotPersisted(f"effect intent could not be committed: {exc}") from exc
    row = get(eid)
    if row is None:
        raise IntentNotPersisted("effect intent was committed but cannot be read back")
    return row


def get(eid: str, *, owner: Optional[str] = None) -> Optional[Dict[str, Any]]:
    try:
        with _db() as conn:
            row = conn.execute("SELECT * FROM effect_outbox WHERE id = ?", (eid,)).fetchone()
    except sqlite3.Error:
        return None
    if row is None or (owner is not None and (row["owner"] or "") != (owner or "")):
        return None
    return _row(row)


def events(eid: str) -> List[Dict[str, Any]]:
    with _db() as conn:
        rows = conn.execute("SELECT * FROM effect_outbox_events WHERE effect_id = ? ORDER BY seq", (eid,)).fetchall()
    out = []
    for r in rows:
        item = {k: r[k] for k in r.keys()}
        try:
            item["detail"] = json.loads(item.pop("detail_json") or "{}")
        except ValueError:
            item["detail"] = {}
        out.append(item)
    return out


def admit(eid: str) -> Dict[str, Any]:
    """Admission gate: the row must be readable through a fresh connection.

    If it cannot be read back the effect must not be sent; the caller gets
    :class:`IntentNotPersisted` and the row (if any) is cancelled with a
    certain "no effect".
    """
    try:
        row = get(eid)
        if row is None or row["state"] != "prepared":
            raise IntentNotPersisted("effect intent is not readable or not in prepared state")
        return _move(eid, "admitted", certainty="none", allowed_from=("prepared",),
                     fields={"admitted_at": _now()})
    except IntentNotPersisted:
        raise
    except (sqlite3.Error, InvalidTransition) as exc:
        raise IntentNotPersisted(f"effect intent could not be admitted: {exc}") from exc


def begin_dispatch(eid: str) -> Dict[str, Any]:
    """Persist ``dispatching`` BEFORE the transport is touched."""
    with _db() as conn:
        row = conn.execute("SELECT dispatch_count FROM effect_outbox WHERE id = ?", (eid,)).fetchone()
    count = int(row["dispatch_count"]) if row else 0
    try:
        return _move(eid, "dispatching", certainty="unknown", allowed_from=("admitted",),
                     fields={"dispatch_started_at": _now(), "dispatch_count": count + 1})
    except (sqlite3.Error, InvalidTransition) as exc:
        raise IntentNotPersisted(f"dispatch could not be recorded: {exc}") from exc


def settle(eid: str, outcome: str, *, reason: str = "", external_ref: str = "",
           error_class: str = "", evidence: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Record what the transport told us. ``outcome`` is one of ``succeeded``,
    ``failed_before_effect``, ``partial``, ``outcome_unknown``."""
    table = {
        "succeeded": "confirmed", "failed_before_effect": "none",
        "partial": "partial", "outcome_unknown": "unknown",
    }
    if outcome not in table:
        raise ValueError(f"cannot settle as {outcome!r}")
    current = get(eid) or {}
    allowed: Sequence[str] = ("dispatching",)
    if outcome == "failed_before_effect" and current.get("state") == "admitted":
        allowed = ("admitted",)
    history = list(current.get("evidence") or [])
    if evidence:
        history.append({"at": _now(), **dict(evidence)})
    return _move(
        eid, outcome, certainty=table[outcome], allowed_from=allowed,
        fields={"settled_at": _now(), "reason": _clip(reason, 1000), "external_ref": _clip(external_ref, 300),
                "error_class": _clip(error_class, 120),
                "evidence_json": json.dumps(history, ensure_ascii=False, default=str)[:8000]},
        detail={"reason": _clip(reason, 300), "error_class": error_class})


def cancel(eid: str, *, reason: str = "cancelled") -> Dict[str, Any]:
    """Cancel. Certainty is ``none`` before dispatch and ``unknown`` while
    dispatching: cancelling is never evidence that the effect did not happen."""
    current = get(eid)
    if current is None:
        raise InvalidTransition(f"unknown effect {eid}")
    during = current["state"] == "dispatching"
    return _move(eid, "cancelled", certainty="unknown" if during else "none",
                 allowed_from=("prepared", "admitted", "dispatching"),
                 fields={"settled_at": _now(), "reason": _clip(reason, 1000)},
                 detail={"during_dispatch": during})


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

_RECONCILERS: Dict[str, Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]] = {}


def register_reconciler(kind: str, finder: Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]) -> None:
    """``finder(record)`` asks the destination about ``record["identifier"]``.

    Returns ``None`` when the destination could not be asked (the row stays
    as it is), ``{"found": True, "external_ref": ...}`` when the effect is
    there, ``{"found": False, "authoritative": True}`` when the destination is
    reachable AND is a source that would hold it (only then does absence
    mean "never landed"), or ``{"found": False}`` otherwise (not found
    *yet*: the row stays unknown and the check is kept as evidence).
    """
    _RECONCILERS[kind] = finder


def reconciler_for(kind: str) -> Optional[Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]]:
    return _RECONCILERS.get(kind)


def reconcile(eid: str, *, owner: Optional[str] = None,
              find: Optional[Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]] = None) -> Dict[str, Any]:
    """Settle an uncertain row by asking the destination. Never re-sends."""
    record = get(eid, owner=owner)
    if record is None:
        return {"ok": False, "reason": "not_found"}
    if record["state"] not in RECONCILABLE_STATES or (
            record["state"] == "cancelled" and record["effect_certainty"] != "unknown"):
        return {"ok": True, "reason": "nothing_to_reconcile", "changed": False, "effect": record}
    finder = find or reconciler_for(record["kind"])
    if finder is None:
        return {"ok": True, "reason": "no_reconciler", "changed": False, "effect": record,
                "detail": "no reconciler is registered for this kind; resolve it manually"}
    if not record["identifier"]:
        return {"ok": True, "reason": "no_identifier", "changed": False, "effect": record,
                "detail": "the effect carried no identifier to look up"}
    try:
        found = finder(dict(record))
    except Exception as exc:  # noqa: BLE001 - the destination being unreachable is not an answer
        logger.warning("effect %s: reconciliation lookup failed: %s", eid, exc)
        found = None
    history = list(record.get("evidence") or [])
    if found is None:
        history.append({"at": _now(), "check": "reconcile", "result": "destination_unreachable"})
        _note_evidence(eid, history)
        return {"ok": True, "reason": "still_unknown", "changed": False, "effect": get(eid)}
    if found.get("found"):
        history.append({"at": _now(), "check": "reconcile", "result": "found",
                        "external_ref": str(found.get("external_ref") or "")})
        updated = _move(
            eid, "reconciled", certainty="confirmed", allowed_from=RECONCILABLE_STATES,
            fields={"reconciled_at": _now(), "resolution": "landed",
                    "external_ref": _clip(found.get("external_ref") or record["identifier"], 300),
                    "evidence_json": json.dumps(history, ensure_ascii=False, default=str)[:8000]},
            detail={"resolution": "landed"})
        return {"ok": True, "reason": "landed", "changed": True, "effect": updated}
    if found.get("authoritative"):
        history.append({"at": _now(), "check": "reconcile", "result": "absent_authoritative"})
        updated = _move(
            eid, "reconciled", certainty="none", allowed_from=RECONCILABLE_STATES,
            fields={"reconciled_at": _now(), "resolution": "not_landed",
                    "evidence_json": json.dumps(history, ensure_ascii=False, default=str)[:8000]},
            detail={"resolution": "not_landed"})
        return {"ok": True, "reason": "not_landed", "changed": True, "effect": updated}
    history.append({"at": _now(), "check": "reconcile", "result": "not_found_yet"})
    _note_evidence(eid, history)
    return {"ok": True, "reason": "not_found_yet", "changed": False, "effect": get(eid),
            "detail": "the destination did not list it, but it is not a source that proves absence"}


def _note_evidence(eid: str, history: List[Dict[str, Any]]) -> None:
    try:
        with _db() as conn:
            conn.execute("UPDATE effect_outbox SET evidence_json = ? WHERE id = ?",
                         (json.dumps(history, ensure_ascii=False, default=str)[:8000], eid))
    except sqlite3.Error:
        logger.debug("effect evidence not stored for %s", eid, exc_info=True)


def resolve_manually(eid: str, *, landed: bool, note: str = "", actor: str = "",
                     owner: Optional[str] = None) -> Dict[str, Any]:
    """A person looked at the destination and decided. Recorded as evidence."""
    record = get(eid, owner=owner)
    if record is None:
        return {"ok": False, "reason": "not_found"}
    if record["state"] not in RECONCILABLE_STATES:
        return {"ok": False, "reason": f"not_reconcilable_from_{record['state']}"}
    history = list(record.get("evidence") or [])
    history.append({"at": _now(), "check": "manual", "landed": bool(landed),
                    "actor": _clip(actor, 120), "note": _clip(note, 500)})
    updated = _move(
        eid, "reconciled", certainty="confirmed" if landed else "none", allowed_from=RECONCILABLE_STATES,
        fields={"reconciled_at": _now(), "resolution": "manual_landed" if landed else "manual_not_landed",
                "evidence_json": json.dumps(history, ensure_ascii=False, default=str)[:8000]},
        detail={"resolution": "manual_landed" if landed else "manual_not_landed"})
    return {"ok": True, "changed": True, "effect": updated}


def reconcile_pending(*, owner: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
    """Ask every registered reconciler about every unresolved row."""
    results = []
    for record in needs_reconciliation(owner=owner, limit=limit):
        if reconciler_for(record["kind"]) is None:
            continue
        results.append({"id": record["id"], **reconcile(record["id"], owner=owner)})
    return results


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

def list_effects(*, owner: Optional[str] = None, state: Optional[str] = None,
                 session_id: Optional[str] = None, run_id: Optional[str] = None,
                 kind: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
    clauses, args = [], []
    for column, value in (("owner", owner), ("state", state), ("session_id", session_id),
                          ("run_id", run_id), ("kind", kind)):
        if value is not None:
            clauses.append(f"{column} = ?")
            args.append(value)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with _db() as conn:
        rows = conn.execute(f"SELECT * FROM effect_outbox{where} ORDER BY created_at DESC LIMIT ?",
                            (*args, max(1, min(int(limit), 500)))).fetchall()
    return [_row(r) for r in rows]


def needs_reconciliation(*, owner: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
    clauses = ["(state IN ('outcome_unknown','partial') OR (state = 'cancelled' AND effect_certainty = 'unknown'))"]
    args: List[Any] = []
    if owner is not None:
        clauses.append("owner = ?")
        args.append(owner)
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM effect_outbox WHERE " + " AND ".join(clauses) + " ORDER BY created_at ASC LIMIT ?",
            (*args, limit)).fetchall()
    return [_row(r) for r in rows]


def recover_orphans() -> Dict[str, int]:
    """Start-up recovery for rows written by an earlier process.

    ``dispatching`` -> ``outcome_unknown`` (the transport may have been
    reached). ``prepared``/``admitted`` -> ``cancelled`` with certainty
    ``none`` (dispatch is persisted before the transport is touched, so the
    effect certainly did not start). Never re-sends anything.
    """
    counts = {"outcome_unknown": 0, "cancelled": 0}
    try:
        with _db() as conn:
            rows = conn.execute(
                "SELECT id, state FROM effect_outbox WHERE state IN ('prepared','admitted','dispatching') "
                "AND boot_id != ?", (BOOT_ID,)).fetchall()
    except sqlite3.Error:
        logger.debug("effect outbox recovery skipped", exc_info=True)
        return counts
    for row in rows:
        try:
            if row["state"] == "dispatching":
                _move(row["id"], "outcome_unknown", certainty="unknown", allowed_from=("dispatching",),
                      fields={"settled_at": _now(), "reason": "the process ended while the effect was dispatching",
                              "error_class": "process_restart"},
                      detail={"recovered": True})
                counts["outcome_unknown"] += 1
            else:
                _move(row["id"], "cancelled", certainty="none", allowed_from=(row["state"],),
                      fields={"settled_at": _now(), "reason": "the process ended before dispatch began",
                              "error_class": "process_restart"},
                      detail={"recovered": True})
                counts["cancelled"] += 1
        except (InvalidTransition, sqlite3.Error):
            continue
    return counts


def summary(*, owner: Optional[str] = None) -> Dict[str, Any]:
    with _db() as conn:
        if owner is None:
            rows = conn.execute("SELECT state, effect_certainty, COUNT(*) n FROM effect_outbox GROUP BY 1,2").fetchall()
        else:
            rows = conn.execute("SELECT state, effect_certainty, COUNT(*) n FROM effect_outbox WHERE owner = ? "
                                "GROUP BY 1,2", (owner,)).fetchall()
    by_state: Dict[str, int] = {}
    for r in rows:
        by_state[r["state"]] = by_state.get(r["state"], 0) + int(r["n"])
    unresolved = sum(by_state.get(s, 0) for s in ("outcome_unknown", "partial"))
    return {"by_state": by_state, "unresolved": unresolved, "total": sum(by_state.values())}


# ---------------------------------------------------------------------------
# Guarded dispatch
# ---------------------------------------------------------------------------

@dataclass
class EffectDispatch:
    """What :func:`dispatch_effect` hands back when the dispatch ran (or was
    refused because an earlier attempt already succeeded)."""
    value: Any = None
    effect: Dict[str, Any] = field(default_factory=dict)
    dispatched: bool = True


#: ``interpret(value)`` may turn a returned value into a verdict:
#: ``("succeeded", {"external_ref": ...})``, ``("failed_before_effect", {...})``,
#: ``("partial", {...})`` or ``("outcome_unknown", {...})``.
Interpreter = Callable[[Any], Optional[tuple]]
#: ``classify(exc)`` lets a transport that knows its own phases say what an
#: exception means: ``"failed_before_effect"`` (the original exception is
#: re-raised unchanged), ``"partial"``, ``"outcome_unknown"``, or ``None`` for
#: the default (unknown, unless the exception is one of this module's markers).
Classifier = Callable[[BaseException], Optional[str]]
#: ``wrap_unknown(effect, exc)`` builds the exception a caller receives for an
#: unknown outcome (so it can also be an instance of the transport's own
#: exception family and keep existing ``except`` clauses working).
UnknownWrapper = Callable[[Dict[str, Any], BaseException], BaseException]


def _open_row(kind: str, *, owner: str, destination: str, identifier: str, identifier_kind: str,
              arguments: Any, arguments_sha256: str, dedup_key: Optional[str], idempotency_key: str,
              destination_enforces_idempotency: bool, context: Optional[EffectContext]) -> Dict[str, Any]:
    ctx = context or current_context() or EffectContext()
    if ctx.admission_id:
        joined = get(ctx.admission_id)
        if joined is not None and joined["state"] == "admitted":
            return joined
    row = prepare(kind=kind, owner=owner, destination=destination, identifier=identifier,
                  identifier_kind=identifier_kind, arguments=arguments, arguments_sha256=arguments_sha256,
                  dedup_key=dedup_key, idempotency_key=idempotency_key,
                  destination_enforces_idempotency=destination_enforces_idempotency, context=ctx)
    if row.get("deduplicated"):
        return row
    return admit(row["id"])


def _verdict_for_exception(exc: BaseException) -> tuple:
    """(outcome, error_class, reason). Anything after dispatch began is
    unknown unless the transport proved otherwise."""
    name = type(exc).__name__
    if isinstance(exc, EffectNotDispatched):
        return "failed_before_effect", name, str(exc)
    if isinstance(exc, EffectPartial):
        return "partial", name, str(exc)
    if isinstance(exc, OutcomeUnknownError):
        return "outcome_unknown", name, str(exc)
    return "outcome_unknown", name, f"{name}: {exc}"


def _dedup_hit(row: Dict[str, Any]) -> EffectDispatch:
    state = row["state"]
    if state in ("succeeded",) or (state == "reconciled" and row["effect_certainty"] == "confirmed"):
        return EffectDispatch(value=None, effect=row, dispatched=False)
    if state == "partial":
        raise PartialEffectError("an earlier attempt of this effect partially landed; not repeating it", effect=row)
    raise OutcomeUnknownError(
        "an earlier attempt of this effect is in flight or its outcome is unknown; not repeating it "
        "(reconcile it first)", effect=row)


def _settle_value(eid: str, value: Any, interpret: Optional[Interpreter]) -> Dict[str, Any]:
    verdict = interpret(value) if interpret else None
    if verdict:
        outcome, info = verdict[0], dict(verdict[1] if len(verdict) > 1 else {})
        return settle(eid, outcome, reason=str(info.get("reason") or ""),
                      external_ref=str(info.get("external_ref") or ""),
                      error_class=str(info.get("error_class") or ""), evidence=info.get("evidence"))
    ref = ""
    if isinstance(value, Mapping):
        ref = str(value.get("external_ref") or value.get("message_id") or value.get("id") or "")
    return settle(eid, "succeeded", external_ref=ref)


def dispatch_effect(kind: str, dispatch: Callable[[], Any], *, owner: str = "", destination: str = "",
                    identifier: str = "", identifier_kind: str = "none", arguments: Any = None,
                    arguments_sha256: str = "", dedup_key: Optional[str] = None,
                    idempotency_key: str = "", destination_enforces_idempotency: bool = False,
                    interpret: Optional[Interpreter] = None,
                    classify: Optional[Classifier] = None,
                    wrap_unknown: Optional[UnknownWrapper] = None,
                    wrap_partial: Optional[UnknownWrapper] = None,
                    context: Optional[EffectContext] = None) -> EffectDispatch:
    """Run ``dispatch`` only after the intent is committed and read back.

    * Intent not persisted -> :class:`IntentNotPersisted`; ``dispatch`` is not called.
    * ``dispatch`` raises :class:`EffectNotDispatched` (or a subclass) -> the
      original exception propagates, the row is ``failed_before_effect``.
    * Any other exception -> the row is ``outcome_unknown`` and
      :class:`OutcomeUnknownError` is raised (``__cause__`` is the original).
    * :class:`EffectPartial` -> ``partial``; :class:`PartialEffectError` is raised.
    * Cancellation while dispatching -> ``cancelled`` with certainty ``unknown``.
    """
    if not enabled():
        return EffectDispatch(value=dispatch(), effect={}, dispatched=True)
    row = _open_row(kind, owner=owner, destination=destination, identifier=identifier,
                    identifier_kind=identifier_kind, arguments=arguments, arguments_sha256=arguments_sha256,
                    dedup_key=dedup_key, idempotency_key=idempotency_key,
                    destination_enforces_idempotency=destination_enforces_idempotency, context=context)
    if row.get("deduplicated"):
        return _dedup_hit(row)
    eid = row["id"]
    begin_dispatch(eid)
    try:
        value = dispatch()
    except BaseException as exc:  # noqa: BLE001 - classification is the whole point
        return _handle_exception(eid, exc, classify, wrap_unknown, wrap_partial)
    return EffectDispatch(value=value, effect=_settle_value(eid, value, interpret), dispatched=True)


async def adispatch_effect(kind: str, dispatch: Callable[[], Awaitable[Any]], *, owner: str = "",
                           destination: str = "", identifier: str = "", identifier_kind: str = "none",
                           arguments: Any = None, arguments_sha256: str = "", dedup_key: Optional[str] = None,
                           idempotency_key: str = "", destination_enforces_idempotency: bool = False,
                           interpret: Optional[Interpreter] = None,
                           classify: Optional[Classifier] = None,
                           wrap_unknown: Optional[UnknownWrapper] = None,
                           wrap_partial: Optional[UnknownWrapper] = None,
                           context: Optional[EffectContext] = None) -> EffectDispatch:
    """Async twin of :func:`dispatch_effect`."""
    if not enabled():
        return EffectDispatch(value=await dispatch(), effect={}, dispatched=True)
    row = await asyncio.to_thread(
        _open_row, kind, owner=owner, destination=destination, identifier=identifier,
        identifier_kind=identifier_kind, arguments=arguments, arguments_sha256=arguments_sha256,
        dedup_key=dedup_key, idempotency_key=idempotency_key,
        destination_enforces_idempotency=destination_enforces_idempotency,
        context=context or current_context())
    if row.get("deduplicated"):
        return _dedup_hit(row)
    eid = row["id"]
    begin_dispatch(eid)
    try:
        value = await dispatch()
    except BaseException as exc:  # noqa: BLE001
        return _handle_exception(eid, exc, classify, wrap_unknown, wrap_partial)
    return EffectDispatch(value=value, effect=_settle_value(eid, value, interpret), dispatched=True)


def _handle_exception(eid: str, exc: BaseException, classify: Optional[Classifier] = None,
                      wrap_unknown: Optional[UnknownWrapper] = None,
                      wrap_partial: Optional[UnknownWrapper] = None) -> EffectDispatch:
    if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
        try:
            cancel(eid, reason=f"{type(exc).__name__} while dispatching")
        except Exception:  # noqa: BLE001
            logger.debug("effect %s: cancel record failed", eid, exc_info=True)
        raise exc
    outcome, error_class, reason = _verdict_for_exception(exc)
    if classify is not None:
        try:
            chosen = classify(exc)
        except Exception:  # noqa: BLE001 - a broken classifier must not claim certainty
            chosen = None
        if chosen in ("failed_before_effect", "partial", "outcome_unknown"):
            outcome = chosen
            reason = f"{error_class}: {exc}"
    try:
        row = settle(eid, outcome, reason=reason, error_class=error_class)
    except Exception:  # noqa: BLE001 - the row is still ``dispatching``; recovery makes it unknown
        logger.warning("effect %s: settle failed after %s", eid, error_class, exc_info=True)
        row = get(eid) or {}
    if outcome == "failed_before_effect":
        raise exc
    if outcome == "partial":
        if wrap_partial is not None:
            wrapped = wrap_partial(row, exc)
            if wrapped is exc:
                raise exc
            raise wrapped from exc
        raise PartialEffectError(reason or "partial effect", effect=row) from exc
    if isinstance(exc, OutcomeUnknownError):
        raise exc
    if wrap_unknown is not None:
        raise wrap_unknown(row, exc) from exc
    raise OutcomeUnknownError(
        f"the outcome of this effect is unknown after {error_class}; it may have been delivered. "
        "Do not send it again without checking.", effect=row) from exc


# ---------------------------------------------------------------------------
# Tool layer helpers (used by execute_tool_block)
# ---------------------------------------------------------------------------

def open_admission(*, kind: str, tool: str, owner: str, destination: str, arguments_sha256: str,
                   session_id: str, run_id: str, call_id: str, dedup: bool = True) -> Dict[str, Any]:
    """Commit + read back the intent of one tool call. Raises
    :class:`IntentNotPersisted` when that is not possible. The caller must
    not invoke the handler in that case."""
    attempt = new_attempt_id()
    ctx = EffectContext(owner=owner, session_id=session_id, run_id=run_id, call_id=call_id,
                        attempt_id=attempt, tool=tool)
    dedup_key = f"{run_id}:{call_id}" if (dedup and run_id and call_id) else None
    row = prepare(kind=kind, owner=owner, destination=destination, arguments_sha256=arguments_sha256,
                  dedup_key=dedup_key, context=ctx)
    if row.get("deduplicated"):
        return row
    return admit(row["id"])


def finalize_admission(eid: str, *, result_status: str, error: str = "", reason: str = "",
                       explicit_not_dispatched: bool = False, external_ref: str = "") -> Dict[str, Any]:
    """Settle a tool-level row from the normalized result.

    ``failed`` without an explicit not-dispatched marker on an already
    dispatching row is ``outcome_unknown``: a generic error after dispatch
    is not evidence that nothing happened.
    """
    row = get(eid)
    if row is None:
        return {}
    state = row["state"]
    if state in ("succeeded", "failed_before_effect", "partial", "outcome_unknown", "reconciled"):
        return row  # a transport inside the handler already settled it precisely
    if state == "admitted":
        # The handler never reached a transport. Whatever it returned, nothing was sent.
        if result_status == "succeeded":
            return cancel(eid, reason="the handler completed without dispatching (for example a draft "
                                      "staged for approval)")
        return settle(eid, "failed_before_effect", reason=reason or error or result_status,
                      error_class="before_dispatch")
    if state == "dispatching":
        if result_status == "succeeded":
            return settle(eid, "succeeded", external_ref=external_ref)
        if result_status == "partial":
            return settle(eid, "partial", reason=reason or error)
        if result_status in ("denied", "conflict") or explicit_not_dispatched:
            return settle(eid, "failed_before_effect", reason=reason or error or result_status)
        return settle(eid, "outcome_unknown", reason=reason or error or result_status,
                      error_class="generic_after_dispatch")
    return row
