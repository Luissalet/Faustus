"""Design canvas history API — versions, semantic diff, decision log (Darwin #989).

Every time a design canvas (OBJ-30, ``src/design_canvas.py``) is filed, the
filing becomes a numbered version (``src/design_canvas_history.py``).

  GET  /api/design-canvas?project_id=           → {canvases: [{id, name, versions, latest, decisions, ...}]}
  GET  /api/design-canvas/{id}                  → {id, name, versions: [{version, ts, author, goal, summary}], decisions}
  GET  /api/design-canvas/{id}/versions/{n}     → one version with its full canvas and markdown
  POST /api/design-canvas/{id}/versions         {canvas, goal?, note?, author?} → the new version (a person's edit)
  GET  /api/design-canvas/{id}/diff?from=&to=   → {changes: [{kind, dimension, ...}], counts, decisions, text}
  GET  /api/design-canvas/{id}/decisions?version=
  POST /api/design-canvas/{id}/decisions        {decision, why?, author?, version?} → the entry
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from src import design_canvas, design_canvas_history as history
from src.auth_helpers import require_user
from src.design_canvas import DesignCanvasError


def _who(request: Request, given: Any) -> str:
    name = str(given or "").strip()
    if name:
        return name[:80]
    try:
        from src.auth_helpers import effective_user
        return str(effective_user(request) or "user")
    except Exception:  # noqa: BLE001
        return "user"


def _int(value: Optional[str]) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, f"not a version number: {value}") from exc


def setup_design_canvas_routes() -> APIRouter:
    router = APIRouter(prefix="/api/design-canvas", tags=["design-canvas"])

    async def call(fn, *args, **kwargs):
        try:
            return await run_in_threadpool(fn, *args, **kwargs)
        except (history.CanvasHistoryError, DesignCanvasError) as exc:
            missing = str(exc).startswith("no design canvas")
            raise HTTPException(404 if missing else 400, str(exc)) from exc

    @router.get("")
    async def list_canvases(request: Request, project_id: str = "") -> Dict[str, Any]:
        require_user(request)
        return {"canvases": await call(history.list_canvases, project_id=project_id.strip())}

    @router.get("/{canvas_id}")
    async def get_history(canvas_id: str, request: Request) -> Dict[str, Any]:
        require_user(request)
        return await call(history.history, canvas_id)

    @router.get("/{canvas_id}/versions/{number}")
    async def get_version(canvas_id: str, number: int, request: Request) -> Dict[str, Any]:
        require_user(request)
        row = await call(history.version, canvas_id, number)
        return {**row, "markdown": design_canvas.render(row.get("canvas") or {})}

    @router.post("/{canvas_id}/versions")
    async def post_version(canvas_id: str, request: Request) -> Dict[str, Any]:
        require_user(request)
        body = await _body(request)
        current = await call(history.get, canvas_id)
        canvas = body.get("canvas")
        if not isinstance(canvas, dict):
            raise HTTPException(400, "send the whole canvas as `canvas`")
        return await call(history.record_version, canvas_id, canvas, goal=str(body.get("goal") or ""),
                          name=str(current.get("name") or ""), author=_who(request, body.get("author")),
                          source="user", note=str(body.get("note") or ""))

    @router.get("/{canvas_id}/diff")
    async def get_diff(canvas_id: str, request: Request) -> Dict[str, Any]:
        require_user(request)
        q = request.query_params
        result = await call(history.diff, canvas_id, _int(q.get("from")), _int(q.get("to")))
        return {**result, "text": history.render_diff(result)}

    @router.get("/{canvas_id}/decisions")
    async def get_decisions(canvas_id: str, request: Request, version: Optional[int] = None) -> Dict[str, Any]:
        require_user(request)
        return {"decisions": await call(history.decisions, canvas_id, version)}

    @router.post("/{canvas_id}/decisions")
    async def post_decision(canvas_id: str, request: Request) -> Dict[str, Any]:
        require_user(request)
        body = await _body(request)
        return await call(history.add_decision, canvas_id, str(body.get("decision") or ""),
                          why=str(body.get("why") or ""), author=_who(request, body.get("author")),
                          version_number=_int(body.get("version")), kind=str(body.get("kind") or "decision"))

    return router


async def _body(request: Request) -> Dict[str, Any]:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    return body if isinstance(body, dict) else {}
