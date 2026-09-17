"""Approval cards over HTTP.

The gating is the design here, and it is not uniform on purpose:

* **reading and requesting** are `require_admin`, which the agent's in-process
  loopback token opens. That is intended — the tool layer is what opens a card
  when a plan needs one, and being able to see what is pending is useful to
  everybody;
* **granting and denying** are `require_human`, which that same token does
  *not* open. An approval the model can grant is a formality, and the model
  reaches admin routes through loopback by design, so the two must be
  different gates rather than the same one used carefully.

`check` is pure and answers with the fields that moved when a granted card no
longer covers a plan, because "approval expired" sends someone hunting for a
bug and "the recipient changed" sends them to the plan.
"""

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from core.middleware import require_admin, require_human
from src import approval_store
from src.contracts import ApprovalPlan, ContractError
from src.contracts.base import now_iso

logger = logging.getLogger(__name__)

# A02: reasons `approval_store.decide()` returns that mean "you lost a race
# to decide this exact card" — as opposed to "no_decider" (this request was
# malformed), "not_found" (no such card), or "expired" (the TTL beat both of
# you). Only these get the 409 in `_decision_response`; a plain 200 with
# `ok: False` stays the shape for everything else, unchanged.
_DECISION_RACE_LOST_REASONS = frozenset({
    "already_granted", "already_denied", "already_expired", "already_revoked",
    "concurrent_change",
})


def _plan_or_400(payload):
    try:
        return ApprovalPlan.parse(payload.get("plan", payload))
    except ContractError as e:
        raise HTTPException(status_code=400,
                            detail=f"{e.path}: {e.message}")


def _current_user(request: Request) -> str:
    return str(getattr(request.state, "current_user", "") or "").strip()


def _read_owner(request: Request, owner_param: str) -> str:
    """Resolve the `owner` GET /pending and /active read with.

    Cookie sessions keep the original design unchanged: `require_admin`
    (opens to the in-process tool token too) and the caller's own `owner`
    query param, because a human reviewing cards is meant to see across
    owners. A bearer `ody_` token is not that admin console: `require_admin`
    would 403 it anyway (it authenticates as the sandboxed pseudo-user "api",
    never a real admin username), and S1.1 does not want a `sessions`-scoped
    token to gain admin-wide visibility even if it could. So a token reads
    only ITS OWN owner's cards — the `owner` query param, if any, is ignored
    for tokens rather than trusted."""
    if getattr(request.state, "api_token", False):
        scopes = set(getattr(request.state, "api_token_scopes", []) or [])
        if "sessions" not in scopes:
            raise HTTPException(403, "API token missing required scope: sessions")
        token_owner = getattr(request.state, "api_token_owner", None)
        if not token_owner:
            raise HTTPException(403, "API token has no owner")
        return token_owner
    require_admin(request)
    return owner_param


def _decision_response(result: dict):
    """A02: the losing side of two concurrent grant/deny calls on the SAME
    card gets a 409, not a quiet 200 — but the body still carries the
    winner's own receipt (`approval`: decided_by/decided_at/status, set by
    `approval_store.decide()` for every one of these reasons), never an
    opaque failure. A caller that only reads `ok`/`reason` (existing UI,
    existing tests) sees no change in shape, only in status code."""
    if not result.get("ok") and str(result.get("reason") or "") in _DECISION_RACE_LOST_REASONS:
        return JSONResponse(status_code=409, content=result)
    return result


def setup_approvals_routes():
    router = APIRouter(prefix="/api/approvals", tags=["approvals"])

    @router.get("/pending")
    def list_pending(request: Request, owner: str = "", limit: int = 50):
        owner = _read_owner(request, owner)
        approval_store.expire_stale()
        cards = approval_store.pending(owner=owner, limit=max(1, min(limit, 200)))
        return {"checked_at": now_iso(),
                "pending": [c.to_dict() for c in cards],
                "count": len(cards)}

    @router.get("/active")
    def list_active(request: Request, owner: str = "", limit: int = 50):
        """SEC-01: the concessions currently in force — granted, not
        expired, with uses left. What a human reviews to see everything a
        model or document could currently claim as "I have permission",
        and revoke from with DELETE /{approval_id}."""
        owner = _read_owner(request, owner)
        cards = approval_store.active(owner=owner, limit=limit)
        return {"checked_at": now_iso(),
                "active": [c.to_dict() for c in cards],
                "count": len(cards)}

    @router.post("/request")
    async def open_card(request: Request):
        """Open a card. Deliberately reachable by the tool layer: asking for
        permission is not the same as giving it."""
        require_admin(request)
        try:
            payload = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Body must be JSON")
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Body must be a JSON object")
        plan = _plan_or_400(payload)
        card = approval_store.request(
            plan,
            owner=str(payload.get("owner") or ""),
            project_id=str(payload.get("project_id") or ""),
            run_id=str(payload.get("run_id") or ""),
            session_id=str(payload.get("session_id") or ""),
            ttl_seconds=payload.get("ttl_seconds", approval_store.DEFAULT_TTL_SECONDS),
            uses=int(payload.get("uses") or 1),
        )
        _notify_approval("approval_pending", card, session_id=str(payload.get("session_id") or ""))
        return {"ok": True, "approval": card.to_dict()}

    @router.post("/check")
    async def check_plan(request: Request):
        """Pure: does a granted card cover this plan right now, and if not,
        which fields moved."""
        require_admin(request)
        try:
            payload = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Body must be JSON")
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Body must be a JSON object")
        plan = _plan_or_400(payload)
        return approval_store.check(plan, owner=str(payload.get("owner") or ""))


    @router.post("/{approval_id}/grant")
    async def grant(approval_id: str, request: Request):
        """A person says yes. `require_human` refuses the agent's loopback
        token here — this is the one endpoint where "who called it" is the
        whole point."""
        require_human(request)
        body = await _optional_json(request)
        result = approval_store.decide(
            approval_id, granted=True,
            by=str(body.get("by") or _current_user(request) or "the signed-in user"),
            reason=str(body.get("reason") or ""))
        _notify_decision(approval_id, "granted", result)
        return _decision_response(result)

    @router.post("/{approval_id}/deny")
    async def deny(approval_id: str, request: Request):
        require_human(request)
        body = await _optional_json(request)
        result = approval_store.decide(
            approval_id, granted=False,
            by=str(body.get("by") or _current_user(request) or "the signed-in user"),
            reason=str(body.get("reason") or ""))
        _notify_decision(approval_id, "denied", result)
        return _decision_response(result)

    @router.delete("/{approval_id}")
    async def revoke(approval_id: str, request: Request):
        """Immediate revocation (SEC-01). `require_human`, same as grant/deny:
        pulling a standing concession is a decision for a person, not
        something the agent's own loopback token can do to itself."""
        require_human(request)
        body = await _optional_json(request)
        result = approval_store.revoke(
            approval_id,
            by=str(body.get("by") or _current_user(request) or "the signed-in user"),
            reason=str(body.get("reason") or ""))
        if not result.get("ok") and result.get("reason") == "not_found":
            raise HTTPException(status_code=404, detail=approval_id)
        return result

    return router


def _notify_approval(kind: str, card, *, session_id: str = "") -> None:
    """Mobile lot M-A: a card just opened. `title` is the tool/action name
    (`plan.action`), `body` the human-readable detail the card shows —
    exactly what a person would need on a lock-screen notification to decide
    whether to open the app right now. Best-effort, never raises."""
    try:
        from src import notifications as _notifications
        plan = card.plan
        _notifications.emit(
            kind,
            owner=card.owner or None,
            title=plan.action or "Approval needed",
            body=plan.detail or ", ".join(plan.recipients) or plan.skill_id or "",
            data=card.to_dict(),
            session_id=session_id or None,
            approval_id=card.id,
        )
    except Exception:
        logger.debug("notifications.emit(%s) failed", kind, exc_info=True)


def _notify_decision(approval_id: str, decision: str, result: dict) -> None:
    """Mobile lot M-A: a person granted or denied a card. Only fires for an
    actual decision — a lost race (409, another decider already won) does
    not get its own duplicate notification."""
    if not result.get("ok"):
        # A failed decide() (not_found, already_*, expired, no_decider) is
        # not a new event to push — the card's own history already reflects
        # it, and the original approval_pending notification is what
        # mattered.
        return
    try:
        from src import notifications as _notifications
        approval = result.get("approval") or {}
        plan = approval.get("plan") or {}
        _notifications.emit(
            "approval_resolved",
            owner=approval.get("owner") or None,
            title=plan.get("action") or "Approval decided",
            body=f"{decision}: {plan.get('detail') or plan.get('skill_id') or ''}".strip(": "),
            data=approval,
            approval_id=approval_id,
        )
    except Exception:
        logger.debug("notifications.emit(approval_resolved) failed", exc_info=True)


async def _optional_json(request: Request) -> dict:
    """A grant with no body is a grant. Requiring one would make the simplest
    call the fiddliest."""
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}
