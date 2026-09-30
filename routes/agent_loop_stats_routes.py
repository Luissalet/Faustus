"""routes/agent_loop_stats_routes.py -- read-only counters of the agent loop's
optional second opinions.

GET /api/agent/advisor/stats
    -> process-wide counters of the advisor (src/advisor.py): calls, per-trigger
       counts, tokens, mean latency and the last few uses WITHOUT their text.

GET /api/agent/decision-forks/stats
    -> counters of the typed decisions at the loop's forks (src/decision_forks.py):
       per fork how often it was asked, decided, fell back, what it chose, what
       came of it, plus the last receipts and which forks are switched on.

GET /api/agent/risk/stats
    -> counters of the risk models declare on state-changing calls
       (src/self_declared_risk.py): how often they declared, how often HIGH
       forced an approval card, how often the declaration and the policy's
       own level disagreed, per tool, plus the last few records.

Nothing here changes anything; it only reports what the loop already did.
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends

from src.auth_helpers import require_user


def setup_agent_loop_stats_routes() -> APIRouter:
    router = APIRouter(prefix="/api/agent", tags=["agent-loop"])

    @router.get("/advisor/stats")
    async def get_advisor_stats(_u: str = Depends(require_user)) -> Dict[str, Any]:
        from src import advisor
        return advisor.stats()

    @router.get("/decision-forks/stats")
    async def get_decision_fork_stats(_u: str = Depends(require_user)) -> Dict[str, Any]:
        from src import decision_forks
        return decision_forks.stats()

    @router.get("/risk/stats")
    async def get_risk_stats(_u: str = Depends(require_user)) -> Dict[str, Any]:
        from src import self_declared_risk
        return self_declared_risk.stats()

    return router


__all__ = ["setup_agent_loop_stats_routes"]
