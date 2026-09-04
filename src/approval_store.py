"""
approval_store.py — the runtime behind `contracts.Approval`.

The contract knows what an approval means. This is what issues one, keeps it,
and answers the only question that matters at the point of use: *does a
granted card still cover what is about to happen?*

The failure mode this is built against is never a forged approval. It is a
plan that drifts one field after the card was signed — a recipient added, a
secret appended, a model that turned out to be a cloud one — while the stored
approval still reads `granted`. So:

* the **whole plan** is stored, not only its hash, and a mismatch is answered
  with the fields that moved;
* an approval is **single use** unless somebody asked otherwise, and consuming
  it is a write, so two runs cannot spend the same yes;
* granting is not something this module can do on behalf of anyone. It takes
  a `decided_by`, and the route above it is gated so the model's own loopback
  token cannot reach it (`core.middleware.require_human`). An approval system
  the agent can call is a formality.

Nothing here decides *whether* something needs approval — that is the
manifest's `effective_approvals()` and the policy above it. This module only
remembers the answer.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Dict, List, Optional, Sequence

from src.contracts import Approval, ApprovalPlan, ContractError
from src.contracts.base import now_iso

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 30 * 60


def _to_row(approval: Approval, *, project_id: str = "", run_id: str = "",
            session_id: str = ""):
    from core.database import ApprovalRow
    return ApprovalRow(
        id=approval.id, status=approval.status, action=approval.plan.action,
        plan_fingerprint=approval.plan.fingerprint(),
        plan_json=json.dumps(approval.plan.to_dict(), ensure_ascii=False, sort_keys=True),
        owner=approval.owner or None, project_id=project_id or None,
        run_id=run_id or None, session_id=session_id or None,
        requested_at=approval.requested_at, decided_at=approval.decided_at,
        decided_by=approval.decided_by or None, expires_at=approval.expires_at,
        uses_left=approval.uses_left, reason=approval.reason or "",
        schema_version=approval.schema_version,
    )


def _from_row(row) -> Approval:
    return Approval.parse({
        "id": row.id, "plan": json.loads(row.plan_json), "status": row.status,
        "owner": row.owner or "", "requested_at": row.requested_at,
        "decided_at": row.decided_at, "decided_by": row.decided_by or "",
        "expires_at": row.expires_at, "uses_left": row.uses_left,
        "reason": row.reason or "",
    })


def _expires(ttl_seconds: Optional[int]) -> Optional[str]:
    """An absolute deadline, computed once. Not sliding: a card that renews
    itself every time someone looks at it never expires, which is the same as
    having no expiry and harder to notice."""
    if ttl_seconds is None:
        return None
    from datetime import datetime, timedelta, timezone
    when = datetime.now(timezone.utc) + timedelta(seconds=max(1, int(ttl_seconds)))
    return when.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def request(plan: Any, *, owner: str = "", project_id: str = "", run_id: str = "",
            session_id: str = "", ttl_seconds: Optional[int] = DEFAULT_TTL_SECONDS,
            uses: int = 1) -> Approval:
    """Open a card. Returns the pending approval; nothing is granted here."""
    from core.database import SessionLocal

    parsed = plan if isinstance(plan, ApprovalPlan) else ApprovalPlan.parse(plan)
    approval = Approval.parse({
        "id": f"apr_{uuid.uuid4().hex[:20]}",
        "plan": parsed.to_dict(),
        "status": "pending",
        "owner": owner,
        "requested_at": now_iso(),
        "expires_at": _expires(ttl_seconds),
        "uses_left": max(1, int(uses)),
    })
    db = SessionLocal()
    try:
        db.add(_to_row(approval, project_id=project_id, run_id=run_id,
                       session_id=session_id))
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return approval


def get(approval_id: str) -> Optional[Approval]:
    from core.database import ApprovalRow, SessionLocal
    db = SessionLocal()
    try:
        row = db.get(ApprovalRow, approval_id)
        return _from_row(row) if row else None
    finally:
        db.close()


def pending(*, owner: str = "", limit: int = 50) -> List[Approval]:
    from core.database import ApprovalRow, SessionLocal
    db = SessionLocal()
    try:
        query = db.query(ApprovalRow).filter(ApprovalRow.status == "pending")
        if owner:
            query = query.filter(ApprovalRow.owner == owner)
        rows = query.order_by(ApprovalRow.requested_at.desc()).limit(limit).all()
        return [_from_row(r) for r in rows]
    finally:
        db.close()


def decide(approval_id: str, *, granted: bool, by: str, reason: str = "") -> Dict[str, Any]:
    """Record a person's answer. `by` is required and is not defaulted: an
    approval whose decider is unknown cannot be audited, and "system" would be
    a lie every time."""
    from core.database import ApprovalRow, SessionLocal

    who = (by or "").strip()
    if not who:
        return {"ok": False, "reason": "no_decider",
                "detail": "an approval has to record who granted it"}

    db = SessionLocal()
    try:
        row = db.get(ApprovalRow, approval_id)
        if row is None:
            return {"ok": False, "reason": "not_found", "detail": approval_id}
        if row.status != "pending":
            # Deliberately not an error: two people clicking the same card is
            # ordinary, and the second one should be told what happened rather
            # than shown a failure.
            return {"ok": False, "reason": f"already_{row.status}",
                    "detail": f"decided by {row.decided_by or 'someone'} "
                              f"at {row.decided_at or 'an unknown time'}"}
        if row.expires_at and now_iso() > row.expires_at:
            row.status = "expired"
            db.commit()
            return {"ok": False, "reason": "expired", "detail": row.expires_at}

        row.status = "granted" if granted else "denied"
        row.decided_at = now_iso()
        row.decided_by = who
        if reason:
            row.reason = reason
        db.commit()
        return {"ok": True, "reason": row.status, "approval": _from_row(row).to_dict()}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def check(plan: Any, *, owner: str = "") -> Dict[str, Any]:
    """Is there a granted card that covers this plan right now?

    Looks up by fingerprint first — an exact match is the common case — and
    only then falls back to the owner's other granted cards for the *same
    action*, so a plan that drifted can be answered with the diff instead of
    "no approval found", which sends the user to open a second identical card
    and wonder why the first one did nothing.
    """
    from core.database import ApprovalRow, SessionLocal

    parsed = plan if isinstance(plan, ApprovalPlan) else ApprovalPlan.parse(plan)
    stamp = now_iso()
    db = SessionLocal()
    try:
        exact = (db.query(ApprovalRow)
                 .filter(ApprovalRow.plan_fingerprint == parsed.fingerprint(),
                         ApprovalRow.status == "granted")
                 .order_by(ApprovalRow.decided_at.desc()).all())
        for row in exact:
            verdict = _from_row(row).covers(parsed, now=stamp)
            if verdict["ok"]:
                return {"ok": True, "approval_id": row.id, "reason": "granted",
                        "changes": []}

        query = db.query(ApprovalRow).filter(
            ApprovalRow.status == "granted", ApprovalRow.action == parsed.action)
        if owner:
            query = query.filter(ApprovalRow.owner == owner)
        near = query.order_by(ApprovalRow.decided_at.desc()).limit(20).all()
        for row in near:
            verdict = _from_row(row).covers(parsed, now=stamp)
            if verdict["reason"] == "plan_changed":
                return {"ok": False, "approval_id": row.id, "reason": "plan_changed",
                        "changes": [dict(c) for c in verdict["changes"]],
                        "detail": "a card was granted for a plan that has since changed"}
        return {"ok": False, "approval_id": "", "reason": "no_approval", "changes": [],
                "detail": f"nothing granted covers this {parsed.action} plan"}
    finally:
        db.close()


def consume(approval_id: str, plan: Any) -> Dict[str, Any]:
    """Spend one use, atomically enough that two runs cannot spend the same
    yes: the plan is re-checked against the stored card **inside** the same
    transaction that decrements it. A `check()` that happened a second ago is
    not evidence at the moment of acting."""
    from core.database import ApprovalRow, SessionLocal

    parsed = plan if isinstance(plan, ApprovalPlan) else ApprovalPlan.parse(plan)
    db = SessionLocal()
    try:
        row = db.get(ApprovalRow, approval_id)
        if row is None:
            return {"ok": False, "reason": "not_found"}
        approval = _from_row(row)
        verdict = approval.covers(parsed)
        if not verdict["ok"]:
            return {"ok": False, "reason": verdict["reason"],
                    "changes": [dict(c) for c in verdict.get("changes", ())]}
        spent = approval.consumed()
        row.uses_left = spent.uses_left
        row.status = spent.status
        db.commit()
        return {"ok": True, "reason": "consumed", "uses_left": row.uses_left,
                "status": row.status}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def expire_stale(*, now: Optional[str] = None) -> int:
    """Mark every pending card past its deadline. Idempotent, and safe to call
    from a scheduler — an expired card is a fact, not a cleanup."""
    from core.database import ApprovalRow, SessionLocal
    stamp = now or now_iso()
    db = SessionLocal()
    try:
        rows = (db.query(ApprovalRow)
                .filter(ApprovalRow.status == "pending",
                        ApprovalRow.expires_at.isnot(None),
                        ApprovalRow.expires_at < stamp).all())
        for row in rows:
            row.status = "expired"
        db.commit()
        return len(rows)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
