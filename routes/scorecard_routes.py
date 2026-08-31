"""Scorecard API — per-model reliability metrics of agent turns (src/scorecard.py).

Admin-gated like the workspace routes: the entries carry workspace paths and
the first line of each request.
"""
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Query, Request

from src.auth_helpers import get_current_user
from src.tool_security import owner_is_admin_or_single_user


def setup_scorecard_routes() -> APIRouter:
    router = APIRouter(prefix="/api/scorecard", tags=["scorecard"])

    def _admin_only(request: Request) -> None:
        if not owner_is_admin_or_single_user(get_current_user(request)):
            raise HTTPException(status_code=403, detail="Admin-only")

    @router.get("")
    def scorecard(
        request: Request,
        days: float = Query(default=30),
        workspace: str = Query(default=""),
        only_workspace: bool = Query(default=True),
        limit: int = Query(default=200),
    ) -> Dict[str, Any]:
        """Per-model table + the most recent raw entries."""
        _admin_only(request)
        from src import scorecard as sc
        entries = sc.load(days=days if days and days > 0 else None)
        if workspace:
            import os
            want = os.path.realpath(os.path.expanduser(workspace))
            entries = [e for e in entries if e.get("workspace") and os.path.realpath(str(e["workspace"])) == want]
        rows = sc.aggregate(entries, only_workspace=only_workspace)
        recent = list(reversed(entries))[: max(1, min(int(limit), 1000))]
        return {"days": days, "models": rows, "entries": recent, "total": len(entries)}

    @router.get("/table")
    def scorecard_table(request: Request, days: float = Query(default=30), language: str = Query(default="en")) -> Dict[str, Any]:
        """Markdown table for the /scorecard slash command."""
        _admin_only(request)
        from src import scorecard as sc
        rows = sc.aggregate(sc.load(days=days if days and days > 0 else None), only_workspace=False)
        return {"markdown": sc.render_table(rows, language=language), "models": len(rows)}

    @router.delete("")
    def clear_scorecard(request: Request) -> Dict[str, Any]:
        _admin_only(request)
        import os
        from src import scorecard as sc
        p = sc._path()
        try:
            if os.path.isfile(p):
                os.remove(p)
        except OSError as e:
            raise HTTPException(status_code=500, detail=str(e))
        return {"ok": True}

    return router
