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


def active(*, owner: str = "", limit: int = 50) -> List[Approval]:
    """SEC-01: the standing concessions a human could pull right now —
    granted, not expired, with uses left. Distinct from `pending()`, which
    lists cards nobody has decided on yet; a card here is one a model or a
    document could currently point to and say "I have permission" — which is
    exactly what needs to be visible (and revocable) independent of the
    model's own claim.
    """
    from core.database import ApprovalRow, SessionLocal

    stamp = now_iso()
    db = SessionLocal()
    try:
        query = db.query(ApprovalRow).filter(
            ApprovalRow.status == "granted", ApprovalRow.uses_left > 0)
        if owner:
            query = query.filter(ApprovalRow.owner == owner)
        rows = query.order_by(ApprovalRow.decided_at.desc()).limit(max(1, min(limit, 200))).all()
        # A granted row past its own TTL is not swept to "expired" the way a
        # never-decided pending card is (expire_stale() only touches
        # "pending") — `covers()`/`consume()` already refuse it live, and
        # "active" should agree rather than list something nothing can use.
        return [_from_row(r) for r in rows if not (r.expires_at and stamp > r.expires_at)]
    finally:
        db.close()


def revoke(approval_id: str, *, by: str, reason: str = "") -> Dict[str, Any]:
    """End a card's ability to authorise anything, right now — SEC-01's
    "revocación inmediata", independent of its TTL or remaining uses.

    Unlike `decide()` this accepts a card in ANY non-terminal status
    (`pending` or `granted`): an open request is exactly the kind of standing
    authority a human should be able to pull before it is ever acted on, not
    only after someone said yes to it. A card already terminal (denied,
    expired, consumed, or already revoked) is left exactly as it is and
    reported honestly, never silently treated as a no-op success.
    """
    from core.database import ApprovalRow, SessionLocal

    who = (by or "").strip()
    if not who:
        return {"ok": False, "reason": "no_decider",
                "detail": "a revocation has to record who revoked it"}

    db = SessionLocal()
    try:
        row = db.get(ApprovalRow, approval_id)
        if row is None:
            return {"ok": False, "reason": "not_found", "detail": approval_id}
        if row.status not in ("pending", "granted"):
            return {"ok": False, "reason": f"already_{row.status}",
                    "detail": f"already {row.status}, nothing to revoke"}
        stamp = now_iso()
        values: Dict[str, Any] = {"status": "revoked", "decided_at": stamp, "decided_by": who}
        if reason:
            values["reason"] = reason
        # CAS on the status this read saw, same shape as decide()'s guard
        # against a concurrent grant/deny landing between the read and this
        # write.
        changed = (db.query(ApprovalRow).filter(
            ApprovalRow.id == approval_id, ApprovalRow.status == row.status,
        ).update(values, synchronize_session=False))
        if not changed:
            db.rollback()
            db.expire_all()
            current = db.get(ApprovalRow, approval_id)
            if current is None:
                return {"ok": False, "reason": "not_found", "detail": approval_id}
            return {"ok": False, "reason": f"already_{current.status}",
                    "detail": "the card changed status before the revocation landed"}
        db.commit()
        answer = _from_row(row).to_dict()
        answer.update(status="revoked", decided_at=stamp, decided_by=who,
                      reason=reason or answer.get("reason", ""))
        return {"ok": True, "reason": "revoked", "approval": answer}
    except Exception:
        db.rollback()
        raise
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
        stamp = now_iso()
        expired = bool(row.expires_at and stamp > row.expires_at)
        values = {"status": "expired" if expired else ("granted" if granted else "denied")}
        if not expired:
            values.update(decided_at=stamp, decided_by=who)
            if reason:
                values["reason"] = reason
        # Preserve exactly the card that was read, not a concurrent edit, a
        # terminal answer, or an expiry written while the person was deciding.
        answer = _from_row(row).to_dict()
        answer.update(values)
        changed = (db.query(ApprovalRow).filter(
            ApprovalRow.id == approval_id, ApprovalRow.status == "pending",
            ApprovalRow.plan_json == row.plan_json,
            ApprovalRow.plan_fingerprint == row.plan_fingerprint,
            ApprovalRow.owner == row.owner, ApprovalRow.expires_at == row.expires_at,
        ).update(values, synchronize_session=False))
        if not changed:
            db.rollback()
            db.expire_all()
            current = db.get(ApprovalRow, approval_id)
            if current is None:
                return {"ok": False, "reason": "not_found", "detail": approval_id}
            return {"ok": False,
                    "reason": (f"already_{current.status}" if current.status != "pending"
                               else "concurrent_change"),
                    "detail": f"decided by {current.decided_by or 'someone'} "
                              f"at {current.decided_at or 'an unknown time'}"}
        db.commit()
        if expired:
            return {"ok": False, "reason": "expired", "detail": answer["expires_at"]}
        return {"ok": True, "reason": answer["status"], "approval": answer}
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
                         ApprovalRow.status == "granted",
                         ApprovalRow.owner == (owner or None))
                 .order_by(ApprovalRow.decided_at.desc()).all())
        for row in exact:
            verdict = _from_row(row).covers(parsed, now=stamp)
            if verdict["ok"]:
                return {"ok": True, "approval_id": row.id, "reason": "granted",
                        "changes": []}

        query = db.query(ApprovalRow).filter(
            ApprovalRow.status == "granted", ApprovalRow.action == parsed.action,
            ApprovalRow.owner == (owner or None))
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


def _spend_row(db, row, parsed: ApprovalPlan, approval: Approval) -> Optional[Approval]:
    """Compare-and-swap one checked card inside the caller's transaction."""
    from core.database import ApprovalRow

    spent = approval.consumed()
    changed = (db.query(ApprovalRow).filter(
        ApprovalRow.id == row.id, ApprovalRow.status == "granted",
        ApprovalRow.uses_left == approval.uses_left,
        ApprovalRow.plan_fingerprint == parsed.fingerprint(),
        ApprovalRow.plan_json == row.plan_json,
        ApprovalRow.owner == row.owner, ApprovalRow.expires_at == row.expires_at,
    ).update({"uses_left": spent.uses_left, "status": spent.status}, synchronize_session=False))
    return spent if changed else None


def consume(approval_id: str, plan: Any, *, owner: Optional[str] = None) -> Dict[str, Any]:
    """Spend one use with a conditional write, rechecking after a lost race.

    Reading then assigning uses_left is not atomic: two workers can read one
    use and both report success. The exact status, plan and remaining uses
    checked below must still hold when the database accepts the decrement.
    """
    from core.database import ApprovalRow, SessionLocal

    parsed = plan if isinstance(plan, ApprovalPlan) else ApprovalPlan.parse(plan)
    db = SessionLocal()
    try:
        for _attempt in range(4):
            row = db.get(ApprovalRow, approval_id)
            if row is None:
                return {"ok": False, "reason": "not_found"}
            if owner is not None and (row.owner or "") != owner:
                return {"ok": False, "reason": "not_found"}
            approval = _from_row(row)
            verdict = approval.covers(parsed)
            if not verdict["ok"]:
                return {"ok": False, "reason": verdict["reason"],
                        "changes": [dict(c) for c in verdict.get("changes", ())]}
            spent = _spend_row(db, row, parsed, approval)
            if spent is not None:
                db.commit()
                return {"ok": True, "reason": "consumed", "uses_left": spent.uses_left,
                        "status": spent.status}
            db.rollback()
            db.expire_all()
        return {"ok": False, "reason": "concurrent_change"}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def consume_plans(plans: Sequence[Any], *, owner: str) -> Dict[str, Any]:
    """Spend all permissions for one execution atomically, or spend none.

    This is an execution boundary, not a preview. Matching is owner-scoped;
    a changed/missing/expired card rolls back every earlier decrement. A lost
    compare-and-swap retries the whole set. A subsequent backend error does
    not refund permissions: an external effect may already have happened.
    """
    from core.database import ApprovalRow, SessionLocal

    parsed_plans = [p if isinstance(p, ApprovalPlan) else ApprovalPlan.parse(p) for p in plans]
    unique = {p.fingerprint(): p for p in parsed_plans}
    db = SessionLocal()
    try:
        for _attempt in range(4):
            receipts = []
            retry = False
            for fingerprint, parsed in unique.items():
                candidates = (db.query(ApprovalRow).filter(
                    ApprovalRow.plan_fingerprint == fingerprint,
                    ApprovalRow.owner == (owner or None),
                    ApprovalRow.status == "granted", ApprovalRow.uses_left > 0,
                ).order_by(ApprovalRow.decided_at.desc(), ApprovalRow.id).all())
                selected = None
                for row in candidates:
                    approval = _from_row(row)
                    if approval.covers(parsed)["ok"]:
                        selected = (row, approval)
                        break
                if selected is None:
                    db.rollback()
                    return {"ok": False, "reason": "no_approval", "action": parsed.action}
                row, approval = selected
                spent = _spend_row(db, row, parsed, approval)
                if spent is None:
                    retry = True
                    break
                receipts.append({"approval_id": approval.id, "action": parsed.action,
                                 "uses_left": spent.uses_left, "status": spent.status})
            if retry:
                db.rollback()
                db.expire_all()
                continue
            db.commit()
            return {"ok": True, "reason": "consumed", "approvals": receipts}
        return {"ok": False, "reason": "concurrent_change"}
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
        changed = (db.query(ApprovalRow)
                .filter(ApprovalRow.status == "pending",
                        ApprovalRow.expires_at.isnot(None),
                        ApprovalRow.expires_at < stamp)
                .update({"status": "expired"}, synchronize_session=False))
        db.commit()
        return changed
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
