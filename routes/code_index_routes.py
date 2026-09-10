"""routes/code_index_routes.py — find_symbol / callers / tests_for over HTTP.

Registered the same way routes/contracts_routes.py registers itself: a
`setup_code_index_routes()` factory returning an `APIRouter`, gated by
`require_admin` on every handler — this is a loopback/diagnostic surface for
the agent and its tooling, not something an anonymous request should reach,
the same posture contracts_routes.py takes for its own read-only endpoints.

Two routes, matching Lote 38's spec exactly:
  * `GET  /api/code-index/{project_id}/symbol?q=` — refresh, then answer with
    the definition(s), lexical callers and candidate tests for `q` in one
    call, so a caller does not need three round-trips for one lookup.
  * `POST /api/code-index/{project_id}/reindex` — bring the index for one
    workspace up to date and report what that cost.

`project_id` is a path segment rather than a query parameter on purpose: it
is the scope every row in `src.code_index`'s tables is keyed by, so a typo'd
project id 404s as "wrong URL" instead of silently returning another
project's symbols because a query parameter was left off.
"""

import logging

from fastapi import APIRouter, HTTPException, Request

from core.middleware import require_admin
from src import code_index

logger = logging.getLogger(__name__)


def setup_code_index_routes():
    router = APIRouter(prefix="/api/code-index", tags=["code-index"])

    @router.get("/{project_id}/symbol")
    def get_symbol(project_id: str, request: Request, q: str = "", path: str = "", kind: str = ""):
        """Definition(s), lexical callers and candidate tests for `q`.

        Refreshes the index first (incremental — see `code_index.refresh`),
        so the answer reflects the workspace as it is on disk right now."""
        require_admin(request)
        symbol = q.strip()
        if not symbol:
            raise HTTPException(status_code=400, detail="q is required")
        code_index.refresh(path, project_id=project_id)
        return {
            "ok": True,
            "project_id": project_id,
            "symbol": symbol,
            "definitions": code_index.find_definition(
                symbol, workspace=path, project_id=project_id, kind=kind),
            "callers": code_index.find_callers(symbol, workspace=path, project_id=project_id),
            "tests": code_index.tests_for(symbol, workspace=path, project_id=project_id),
        }

    @router.post("/{project_id}/reindex")
    async def reindex(project_id: str, request: Request):
        """Bring the index up to date for one workspace and say what that cost."""
        require_admin(request)
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        workspace = str(payload.get("path") or payload.get("workspace") or "")
        result = code_index.refresh(
            workspace, project_id=project_id, full=bool(payload.get("full") or False))
        return {"ok": True, "project_id": project_id, "refresh": result}

    return router
