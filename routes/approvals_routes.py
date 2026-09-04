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

from core.middleware import require_admin, require_human
from src import approval_store
from src.contracts import ApprovalPlan, ContractError
from src.contracts.base import now_iso

logger = logging.getLogger(__name__)


def _plan_or_400(payload):
    try:
        return ApprovalPlan.parse(payload.get("plan", payload))
    except ContractError as e:
        raise HTTPException(status_code=400,
                            detail=f"{e.path}: {e.message}")


def _current_user(request: Request) -> str:
    return str(getattr(request.state, "current_user", "") or "").strip()


def setup_approvals_routes():
    router = APIRouter(prefix="/api/approvals", tags=["approvals"])

    @router.get("/pending")
    def list_pending(request: Request, owner: str = "", limit: int = 50):
        require_admin(request)
        approval_store.expire_stale()
        cards = approval_store.pending(owner=owner, limit=max(1, min(limit, 200)))
        return {"checked_at": now_iso(),
                "pending": [c.to_dict() for c in cards],
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
        return result

    @router.post("/{approval_id}/deny")
    async def deny(approval_id: str, request: Request):
        require_human(request)
        body = await _optional_json(request)
        return approval_store.decide(
            approval_id, granted=False,
            by=str(body.get("by") or _current_user(request) or "the signed-in user"),
            reason=str(body.get("reason") or ""))

    return router


async def _optional_json(request: Request) -> dict:
    """A grant with no body is a grant. Requiring one would make the simplest
    call the fiddliest."""
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}
