"""Creator Goal routes (WP27): Goal with completion by evidence, over HTTP.

``src/creator/goal.py`` is the sole authority; this module is a thin HTTP
adapter, same shape as ``routes/creator_routes.py``/``creator_preflight_routes.py``.
Every route is gated by the ``creator_enabled`` setting (default False,
``src/settings.py``), checked BEFORE any store access (CONTRATO.md rule 5).

Owner is always resolved from the authenticated session, never from the
request body (CONTRATO.md rule 3); a goal that exists but belongs to
someone else answers identically to one that does not exist: 404.

The model cannot mark a goal done through these routes either — POST
``.../evaluate`` always calls ``src.creator.goal.evaluate``, which re-runs
every real checker itself; there is no "set status" route.
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request

from src.auth_helpers import require_user


def _owner(request: Request) -> str:
    from core import middleware as mw
    from src.owner_identity import effective_storage_owner
    user = require_user(request)
    owner = effective_storage_owner(user, auth_is_disabled=mw.auth_disabled())
    return owner or ""


def _creator_enabled() -> bool:
    from src.settings import get_setting
    return bool(get_setting("creator_enabled", False))


def _require_flag() -> None:
    if not _creator_enabled():
        raise HTTPException(status_code=404, detail="creator is not enabled")


async def _json_object(request: Request) -> Dict[str, Any]:
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "Request body must be JSON")
    if not isinstance(payload, dict):
        raise HTTPException(400, "Request body must be a JSON object")
    return payload


def _goal_error(exc: Exception):
    from src.creator.goal import GoalNotFound, InvalidGoal, GoalRevisionConflict
    if isinstance(exc, GoalNotFound):
        raise HTTPException(404, "goal not found")
    if isinstance(exc, GoalRevisionConflict):
        raise HTTPException(409, {"reason": "revision_conflict", "current_revision": exc.current_revision})
    if isinstance(exc, InvalidGoal):
        raise HTTPException(400, str(exc))
    raise exc


def setup_creator_goal_routes() -> APIRouter:
    router = APIRouter(prefix="/api/creator", tags=["creator"])

    @router.get("/goals")
    def list_goals(request: Request, project_id: str = ""):
        owner = _owner(request)
        _require_flag()
        if not project_id:
            raise HTTPException(400, "project_id is required")
        from src.creator.goal import get_store
        goals = get_store().list_for_project(owner, project_id)
        return {"goals": [g.to_public_dict() for g in goals]}

    @router.post("/goals")
    async def create_goal(request: Request):
        owner = _owner(request)
        _require_flag()
        payload = await _json_object(request)
        project_id = str(payload.get("project_id") or "")
        statement = str(payload.get("statement") or "")
        acceptance = payload.get("acceptance")
        if not project_id:
            raise HTTPException(400, "project_id is required")
        if not isinstance(acceptance, list) or not acceptance:
            raise HTTPException(400, "acceptance must be a non-empty list of criteria")

        from services.projects import get_store as get_project_store
        if get_project_store().get(project_id, owner) is None:
            raise HTTPException(404, "project not found")

        from src.creator.goal import get_store, InvalidGoal
        try:
            goal = get_store().define(
                owner, project_id, statement, acceptance,
                floor=payload.get("floor"), ceiling=payload.get("ceiling"),
                no_progress_limit=int(payload.get("no_progress_limit") or 2),
            )
        except InvalidGoal as exc:
            raise HTTPException(400, str(exc))
        return goal.to_public_dict()

    @router.get("/goals/{goal_id}")
    def get_goal(request: Request, goal_id: str):
        owner = _owner(request)
        _require_flag()
        from src.creator.goal import get_store, continue_step
        goal = get_store().get(owner, goal_id)
        if goal is None:
            raise HTTPException(404, "goal not found")
        return {**goal.to_public_dict(), "continue_step": continue_step(goal)}

    @router.post("/goals/{goal_id}/evaluate")
    async def evaluate_goal(request: Request, goal_id: str):
        owner = _owner(request)
        _require_flag()
        payload: Dict[str, Any] = {}
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                payload = {}
        except Exception:
            payload = {}
        workspace = payload.get("workspace")

        from src.creator import goal as goal_mod
        store = goal_mod.get_store()
        try:
            report = goal_mod.evaluate(store, owner, goal_id, workspace=workspace)
        except Exception as exc:  # narrowed by _goal_error
            _goal_error(exc)
            raise
        goal = store.get(owner, goal_id)
        return {
            "report": report.to_dict(),
            "goal": goal.to_public_dict() if goal is not None else None,
            "continue_step": goal_mod.continue_step(goal) if goal is not None else None,
        }

    @router.post("/goals/{goal_id}/abandon")
    async def abandon_goal(request: Request, goal_id: str):
        owner = _owner(request)
        _require_flag()
        payload: Dict[str, Any] = {}
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                payload = {}
        except Exception:
            payload = {}
        from src.creator.goal import get_store
        try:
            goal = get_store().abandon(owner, goal_id, str(payload.get("reason") or ""))
        except Exception as exc:  # narrowed by _goal_error
            _goal_error(exc)
            raise
        return goal.to_public_dict()

    return router
