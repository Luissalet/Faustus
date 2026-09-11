"""ADP-11 — GET /api/attention, POST /api/attention/read.

The transport for `src/attention.py`: owner-scoped (same `effective_user`
every neighbouring route in this file's family uses — see
`routes/chat_routes.py::chat_activity`/`open_questions`), read-only for the
classification itself. Nothing here grants, denies or answers anything — an
approval/question surfaced here is still resolved through the SAME routes
Activity already uses (`POST /api/approvals/{id}/...`, `sendTurn` with
`questionId`), exactly as the ADP-11 ficha requires ("no dupliques").
"""
from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from src import attention
from src.auth_helpers import effective_user


def _error(status: int, error_class: str, detail: str | None = None, **extra: Any) -> JSONResponse:
    """Same flat shape as `routes/git_routes.py::_error`: `error_class` next
    to `detail` at the top level, not nested under FastAPI's default
    `{"detail": ...}` wrapping."""
    body: Dict[str, Any] = {"error_class": error_class}
    if detail is not None:
        body["detail"] = detail
    body.update(extra)
    return JSONResponse(status_code=status, content=body)


def setup_attention_routes() -> APIRouter:
    router = APIRouter(tags=["attention"])

    @router.get("/api/attention")
    async def get_attention(request: Request, limit: int = 50) -> Dict[str, Any]:
        owner = effective_user(request) or ""
        runs = attention.attention_for_owner(owner, limit=limit)
        return {"runs": runs, "unread_count": sum(1 for r in runs if r["unread"])}

    @router.post("/api/attention/read")
    async def mark_attention_read(request: Request) -> Dict[str, Any]:
        owner = effective_user(request) or ""
        try:
            body = await request.json()
        except Exception:
            body = {}
        raw_ids = (body or {}).get("run_ids")
        if not isinstance(raw_ids, list):
            return _error(400, "attention.invalid_run_ids", "run_ids must be a list of session ids")
        ids: List[str] = [str(v) for v in raw_ids if isinstance(v, (str, int)) and str(v).strip()][:500]
        marked = attention.mark_read(owner, ids)
        return {"ok": True, "marked": marked}

    return router
