"""routes/code_history_routes.py — HTTP surface for git history understanding
(Lot F). Thin wrapper over `src.code_history`, same auth/confinement pattern
as the neighbouring `routes/code_graph_routes.py`: `require_admin` on the
endpoint, workspace confinement enforced by `code_history`'s own resolver
(a `workspace` outside the allowed roots is a 400, not a 500).

GET /api/code-history ?workspace=&path=&symbol=&mode=&limit=&start=&end=
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query, Request

from core.middleware import require_admin
from src import code_history

logger = logging.getLogger(__name__)

_MODES = {"explain", "file", "symbol", "blame", "co_change", "risk"}


def _resolved_workspace(raw: str) -> str:
    from src.tool_execution import _resolve_search_root
    return _resolve_search_root(raw or "")


def setup_code_history_routes():
    router = APIRouter(prefix="/api/code-history")

    @router.get("")
    def code_history_endpoint(
        request: Request,
        workspace: str = "",
        path: str = Query(...),
        symbol: str = "",
        mode: str = "explain",
        limit: int = 0,
        start: int = 0,
        end: int = 0,
    ):
        require_admin(request)
        try:
            root = _resolved_workspace(workspace)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        chosen_mode = mode.strip().lower() if mode.strip().lower() in _MODES else "explain"
        try:
            if chosen_mode == "file":
                result = code_history.file_history(root, path, limit=limit or 30)
            elif chosen_mode == "symbol":
                if not symbol:
                    raise HTTPException(status_code=400, detail="mode=symbol requires `symbol`")
                result = code_history.symbol_history(root, path, symbol, limit=limit or 20)
            elif chosen_mode == "blame":
                result = code_history.blame_summary(
                    root, path, start=start or None, end=end or None,
                )
            elif chosen_mode == "co_change":
                result = code_history.co_change(root, path, limit=limit or 200)
            elif chosen_mode == "risk":
                result = code_history.risk(root, path)
            else:
                result = code_history.explain(root, path, symbol=symbol or None)
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("code_history route failed: %s", exc)
            raise HTTPException(status_code=500, detail=str(exc))

        if isinstance(result, dict) and result.get("error"):
            raise HTTPException(status_code=400, detail=result["error"])
        return result

    return router
