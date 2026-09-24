"""routes/approval_autonomy_routes.py — read-only stats plus manual
promote/demote for `src.approval_autonomy` (shadow mode / confidence-tiered
auto-approval, feature 2). Same auth posture as the neighbouring approval
surface: reading stats is `require_admin` (an operator or the in-process
agent loopback token looking at what it would decide), changing the mode
setting or a family's promotion status is `require_human` (a decision about
what the agent gets to auto-approve is not one the model makes about
itself).

GET  /api/approval-autonomy/mode              -- current setting + thresholds
GET  /api/approval-autonomy/stats  ?owner=    -- per-family shadow-log stats
POST /api/approval-autonomy/family            -- {owner, family, status}
                                                  status: "promoted" | "demoted" | ""
"""
import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from core.middleware import require_admin, require_human

logger = logging.getLogger(__name__)


class FamilyOverrideBody(BaseModel):
    owner: str = ""
    family: str
    status: str = ""  # "promoted" | "demoted" | "" (clear)


def setup_approval_autonomy_routes():
    router = APIRouter(prefix="/api/approval-autonomy")

    @router.get("/mode")
    def mode(request: Request):
        require_admin(request)
        from src import approval_autonomy as autonomy
        return {
            "mode": autonomy.autonomy_mode(),
            "modes": list(autonomy.MODES),
            "act_threshold": autonomy.ACT_THRESHOLD,
            "advise_threshold": autonomy.ADVISE_THRESHOLD,
            "promote_min_decisions": autonomy.DEFAULT_PROMOTE_MIN_DECISIONS,
            "promote_min_agreement": autonomy.DEFAULT_PROMOTE_MIN_AGREEMENT,
        }

    @router.get("/stats")
    def stats(request: Request, owner: str = ""):
        require_admin(request)
        from src import approval_autonomy as autonomy
        try:
            return {"families": autonomy.family_stats(owner)}
        except Exception as exc:  # noqa: BLE001
            logger.warning("approval-autonomy stats failed: %s", exc)
            raise HTTPException(status_code=500, detail=str(exc))

    @router.post("/family")
    def set_family(body: FamilyOverrideBody, request: Request):
        require_human(request)
        from src import approval_autonomy as autonomy
        try:
            autonomy.set_family_override(body.owner, body.family, body.status or None)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.warning("approval-autonomy family override failed: %s", exc)
            raise HTTPException(status_code=500, detail=str(exc))
        return {"ok": True, "owner": body.owner, "family": body.family, "status": body.status or None}

    return router
