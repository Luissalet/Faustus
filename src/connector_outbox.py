"""
connector_outbox.py — read / prepare / execute, for any tool with an effect
that leaves Faustus (CONN-02, spec docs/spec/v2/Faustus_Especificacion_Integral_v2.md).

The failure this exists to stop is the one `src/workflows/store.py` and
`src/media_runs.py` already stop for workflows and renders: a caller that
CANNOT TELL, after a crash or a dropped connection, whether the outside world
already saw the effect. A tool that reads (`GET /search`, `GET /resolve-contact`)
never faces that question — nothing it does needs to be told apart from a
retry. A tool that acts on the world does, and this module is the one place
that question is answered for every connector, instead of once per connector
author who remembers to think about it:

* **`prepare`** builds the payload, fingerprints it, and opens an
  `ApprovalRequest` via `src.approval_store` — the SAME authority chat tool
  approvals already use, not a second one. Nothing external happens yet.
* **`execute`** may only run against a `prepared` row with a still-valid
  approval whose plan still matches the stored payload byte-for-byte
  (`payload_digest` is folded into the `ApprovalPlan.detail` fingerprinted by
  `approval_store`, so editing the payload after prepare silently invalidates
  the approval exactly the way a changed recipient or secret does).
* A `run()` callable that cannot tell whether its effect landed raises
  `ConnectorEffectUncertain` instead of returning normally or raising a plain
  exception. The row becomes `uncertain`, never `failed` and never retried
  automatically — QA-11's `outcome_unknown` for connectors. Only
  **`reconcile`** may move a row out of `uncertain`, and only by asking the
  destination itself (Message-ID in Sent, a calendar UID, ...), never by
  resending.

Storage is a dedicated SQLite file, the same pattern `routes/email_helpers.py`
already uses for `scheduled_emails` (`sqlite3` directly, no ORM) rather than a
new table on the shared `core.database` `Base` — this module owns its own
migration and does not need a change to `core/database.py` to exist.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from src.constants import DATA_DIR

logger = logging.getLogger(__name__)

OUTBOX_DB = os.path.join(DATA_DIR, "connector_outbox.db")

#: `prepared` — payload written, approval opened, nothing external happened.
#: `executing` — a `run()` is in flight; a process that dies here leaves the
#:   row here forever, which is visible and searchable rather than silently
#:   looking like nothing was ever attempted.
#: `executed` — the destination confirmed; `external_ref` names what it gave
#:   back (a Message-ID, a calendar UID, ...).
#: `uncertain` — QA-11's `outcome_unknown`: the destination may have acted,
#:   nobody saw the confirmation. Only `reconcile()` may leave this state.
#: `failed` — the destination spoke and refused, or `reconcile()` confirmed
#:   the effect never landed.
#: `cancelled` — withdrawn before execution.
STATUSES = ("prepared", "executing", "executed", "uncertain", "failed", "cancelled")

#: `execute()` may only be called against these. Notably NOT `uncertain`:
#: nothing in this module ever turns an ambiguous attempt into a second one.
_EXECUTABLE_FROM = ("prepared",)


class ConnectorEffectUncertain(Exception):
    """Raise this from a `run()` callable passed to `execute()` when the
    destination may have taken the action but the confirmation was lost —
    a socket that died after SMTP DATA was sent, a calendar API call that
    timed out waiting on the response. Any OTHER exception from `run()` is
    read as the destination having refused or never having been reached,
    which is a real, retryable failure rather than an open question."""


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(OUTBOX_DB), exist_ok=True)
    conn = sqlite3.connect(OUTBOX_DB, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS connector_outbox (
            id TEXT PRIMARY KEY,
            owner TEXT NOT NULL DEFAULT '',
            connector TEXT NOT NULL,
            action TEXT NOT NULL,
            idempotency_key TEXT,
            payload_json TEXT NOT NULL,
            payload_digest TEXT NOT NULL,
            approval_id TEXT NOT NULL,
            recipients_json TEXT NOT NULL DEFAULT '[]',
            preview TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'prepared',
            external_ref TEXT,
            reason TEXT NOT NULL DEFAULT '',
            attempts INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            executed_at TEXT,
            schema_version INTEGER NOT NULL DEFAULT 1
        )
    """)
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_connector_outbox_idem "
        "ON connector_outbox(idempotency_key) WHERE idempotency_key IS NOT NULL"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_connector_outbox_owner_status "
        "ON connector_outbox(owner, status)"
    )
    conn.commit()
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def payload_digest(payload: Mapping[str, Any]) -> str:
    """A stable digest of the exact payload a preview was shown for.

    Folded into the `ApprovalPlan.detail` the approval is opened with, so —
    without any change to `approval_store` — editing the payload after
    `prepare()` changes the plan's fingerprint and `covers()` reports
    `plan_changed` exactly as it does for a changed recipient list."""
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"], "owner": row["owner"], "connector": row["connector"],
        "action": row["action"], "idempotency_key": row["idempotency_key"] or "",
        "payload": json.loads(row["payload_json"]),
        "payload_digest": row["payload_digest"], "approval_id": row["approval_id"],
        "recipients": json.loads(row["recipients_json"] or "[]"),
        "preview": row["preview"] or "", "status": row["status"],
        "external_ref": row["external_ref"] or "", "reason": row["reason"] or "",
        "attempts": row["attempts"], "created_at": row["created_at"],
        "executed_at": row["executed_at"],
    }


def _build_plan(payload: Mapping[str, Any], *, action: str, digest: str,
                 recipients: Sequence[str], preview: str):
    from src.contracts import ApprovalPlan
    return ApprovalPlan.parse({
        "action": action,
        "recipients": list(recipients),
        "detail": f"digest:{digest} {preview}"[:2000],
    })


def prepare(*, connector: str, action: str, owner: str, payload: Mapping[str, Any],
            recipients: Sequence[str] = (), preview: str = "",
            idempotency_key: str = "", ttl_seconds: int = 1800,
            approval_action: str = "deliver") -> Dict[str, Any]:
    """Build the payload, fingerprint it, and open the `ApprovalRequest`.

    Nothing external happens here. `approval_action` is one of
    `src.contracts.skill.APPROVAL_TRIGGERS` (`"deliver"` for anything sent to
    a third party — email, a posted message, a calendar invite).
    """
    payload = dict(payload)
    digest = payload_digest(payload)
    conn = _connect()
    try:
        if idempotency_key:
            existing = conn.execute(
                "SELECT * FROM connector_outbox WHERE idempotency_key = ? AND owner = ?",
                (idempotency_key, owner or ""),
            ).fetchone()
            if existing is not None:
                if existing["payload_digest"] == digest:
                    # The same request, prepared again (a retried click, a
                    # retried request) — hand back the row that already
                    # exists rather than open a second approval for it.
                    return {"ok": True, "reused": True, **_row_to_dict(existing)}
                return {"ok": False, "reason": "idempotency_key_conflict",
                        "detail": "this idempotency_key was already prepared for a "
                                  "different payload"}

        plan = _build_plan(payload, action=approval_action, digest=digest,
                            recipients=recipients, preview=preview)
        from src import approval_store
        approval = approval_store.request(plan, owner=owner, ttl_seconds=ttl_seconds)

        outbox_id = f"cob_{uuid.uuid4().hex[:20]}"
        now = _now()
        conn.execute(
            "INSERT INTO connector_outbox (id, owner, connector, action, "
            "idempotency_key, payload_json, payload_digest, approval_id, "
            "recipients_json, preview, status, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (outbox_id, owner or "", connector, action, idempotency_key or None,
             json.dumps(payload, ensure_ascii=False), digest, approval.id,
             json.dumps(list(recipients), ensure_ascii=False), preview,
             "prepared", now),
        )
        conn.commit()
        return {"ok": True, "reused": False, "outbox_id": outbox_id,
                "approval_id": approval.id, "payload_digest": digest,
                "preview": preview, "status": "prepared",
                "expires_at": approval.expires_at}
    finally:
        conn.close()


def get(outbox_id: str, *, owner: Optional[str] = None) -> Optional[Dict[str, Any]]:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM connector_outbox WHERE id = ?", (outbox_id,)).fetchone()
        if row is None:
            return None
        if owner is not None and (row["owner"] or "") != (owner or ""):
            return None
        return _row_to_dict(row)
    finally:
        conn.close()


def _set_status(conn: sqlite3.Connection, outbox_id: str, *, from_statuses: Sequence[str],
                 **fields: Any) -> bool:
    placeholders = ",".join("?" for _ in from_statuses)
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    cur = conn.execute(
        f"UPDATE connector_outbox SET {set_clause} WHERE id = ? AND status IN ({placeholders})",
        (*fields.values(), outbox_id, *from_statuses),
    )
    conn.commit()
    return cur.rowcount > 0


def execute(outbox_id: str, *, owner: str,
            run: Callable[[Dict[str, Any]], Dict[str, Any]]) -> Dict[str, Any]:
    """Spend the approval and run the effect exactly once.

    `run(payload)` performs the real call and returns a mapping with at least
    `{"ok": True, "external_ref": "..."}` on confirmed success, or raises:
    `ConnectorEffectUncertain` when the destination may have acted but the
    confirmation was lost, or any other exception when it did not (refused,
    unreachable before anything was sent). The row is written BEFORE `run`
    is called (`executing`) and AFTER it returns, the same ordering
    `src/workflows/store.py` uses for a node's own claim/result."""
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM connector_outbox WHERE id = ?", (outbox_id,)).fetchone()
        if row is None or (row["owner"] or "") != (owner or ""):
            return {"ok": False, "reason": "not_found"}
        record = _row_to_dict(row)
        if record["status"] == "uncertain":
            return {"ok": False, "reason": "needs_reconciliation", "outbox_id": outbox_id,
                    "detail": "a previous attempt's outcome is unknown; call reconcile() "
                              "before trying again — never a blind resend"}
        if record["status"] not in _EXECUTABLE_FROM:
            return {"ok": False, "reason": f"already_{record['status']}", "outbox_id": outbox_id}

        plan = _build_plan(record["payload"], action=_plan_action(record), digest=record["payload_digest"],
                            recipients=record["recipients"], preview=record["preview"])
        from src import approval_store
        spent = approval_store.consume(record["approval_id"], plan, owner=owner)
        if not spent.get("ok"):
            return {"ok": False, "reason": spent.get("reason", "approval_not_usable"),
                     "changes": spent.get("changes", []), "outbox_id": outbox_id}

        claimed = _set_status(conn, outbox_id, from_statuses=_EXECUTABLE_FROM,
                               status="executing", attempts=record["attempts"] + 1)
        if not claimed:
            return {"ok": False, "reason": "concurrent_execution", "outbox_id": outbox_id}
    finally:
        conn.close()

    try:
        result = run(dict(record["payload"]))
    except ConnectorEffectUncertain as e:
        conn = _connect()
        try:
            _set_status(conn, outbox_id, from_statuses=("executing",), status="uncertain",
                        reason=f"outcome_unknown: {e}")
        finally:
            conn.close()
        logger.warning("connector outbox %s: outcome unknown (%s); reconcile before retrying",
                       outbox_id, e)
        return {"ok": False, "reason": "outcome_unknown", "outbox_id": outbox_id, "detail": str(e)}
    except Exception as e:
        conn = _connect()
        try:
            _set_status(conn, outbox_id, from_statuses=("executing",), status="failed",
                        reason=f"{type(e).__name__}: {e}")
        finally:
            conn.close()
        return {"ok": False, "reason": "failed", "outbox_id": outbox_id, "detail": str(e)}

    if not isinstance(result, Mapping) or not result.get("ok"):
        detail = (result or {}).get("reason") or (result or {}).get("detail") or "run() reported failure"
        conn = _connect()
        try:
            _set_status(conn, outbox_id, from_statuses=("executing",), status="failed", reason=str(detail))
        finally:
            conn.close()
        return {"ok": False, "reason": "failed", "outbox_id": outbox_id, "detail": str(detail)}

    external_ref = str(result.get("external_ref") or "")
    conn = _connect()
    try:
        _set_status(conn, outbox_id, from_statuses=("executing",), status="executed",
                    external_ref=external_ref, executed_at=_now(), reason="")
    finally:
        conn.close()
    return {"ok": True, "outbox_id": outbox_id, "status": "executed", "external_ref": external_ref}


def _plan_action(record: Mapping[str, Any]) -> str:
    # Same choice `prepare()` made — recovered from the connector/action pair
    # rather than stored twice, since `ApprovalPlan.action` is one of a fixed
    # vocabulary (`src.contracts.skill.APPROVAL_TRIGGERS`) and every connector
    # this module serves today reaches a third party ("deliver").
    return "deliver"


def reconcile(outbox_id: str, *, owner: str,
              find: Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]) -> Dict[str, Any]:
    """Settle an `uncertain` row by asking the destination, never by resending.

    `find(payload)` must look the effect up by an identifying key the payload
    already carries (a Message-ID, a calendar UID) and return `None` when the
    destination could not be reached (leaves the row `uncertain` — undecided
    is not the same as absent), `{"found": False}` when it was reachable and
    genuinely holds nothing matching (the row becomes `failed`: it never
    landed), or `{"found": True, "external_ref": ...}` when it does (the row
    is adopted as `executed`, mirroring `src/media_runs.py::reconcile_run`).
    """
    record = get(outbox_id, owner=owner)
    if record is None:
        return {"ok": False, "reason": "not_found"}
    if record["status"] != "uncertain":
        return {"ok": True, "reason": "nothing_to_reconcile", "status": record["status"],
                "outbox_id": outbox_id, "changed": False}

    found = find(dict(record["payload"]))
    conn = _connect()
    try:
        if found is None:
            return {"ok": True, "reason": "still_uncertain", "outbox_id": outbox_id, "changed": False}
        if found.get("found"):
            ref = str(found.get("external_ref") or "")
            changed = _set_status(conn, outbox_id, from_statuses=("uncertain",), status="executed",
                                   external_ref=ref, executed_at=_now(),
                                   reason="adopted by reconciliation")
            return {"ok": True, "reason": "adopted", "outbox_id": outbox_id,
                    "external_ref": ref, "changed": changed}
        changed = _set_status(conn, outbox_id, from_statuses=("uncertain",), status="failed",
                               reason="reconciliation found no matching record at the "
                                      "destination; the effect never landed")
        return {"ok": True, "reason": "never_landed", "outbox_id": outbox_id, "changed": changed}
    finally:
        conn.close()


def cancel(outbox_id: str, *, owner: str) -> Dict[str, Any]:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM connector_outbox WHERE id = ?", (outbox_id,)).fetchone()
        if row is None or (row["owner"] or "") != (owner or ""):
            return {"ok": False, "reason": "not_found"}
        changed = _set_status(conn, outbox_id, from_statuses=("prepared",), status="cancelled",
                               reason="cancelled before execution")
        if not changed:
            current = get(outbox_id, owner=owner) or {}
            return {"ok": current.get("status") == "cancelled", "reason":
                     "already_" + str(current.get("status"))}
        return {"ok": True, "outbox_id": outbox_id, "status": "cancelled"}
    finally:
        conn.close()


def needs_reconciliation(*, owner: str = "") -> List[Dict[str, Any]]:
    """The rows `reconcile()` exists to settle — the QA-11 queue for connectors."""
    conn = _connect()
    try:
        query = "SELECT * FROM connector_outbox WHERE status = 'uncertain'"
        args: tuple = ()
        if owner:
            query += " AND owner = ?"
            args = (owner,)
        rows = conn.execute(query + " ORDER BY created_at ASC", args).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()
