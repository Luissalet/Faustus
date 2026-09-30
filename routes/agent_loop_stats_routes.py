"""routes/agent_loop_stats_routes.py -- read-only counters of the agent loop's
optional second opinions.

GET /api/agent/advisor/stats
    -> process-wide counters of the advisor (src/advisor.py): calls, per-trigger
       counts, tokens, mean latency and the last few uses WITHOUT their text.

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

    return router


__all__ = ["setup_agent_loop_stats_routes"]
