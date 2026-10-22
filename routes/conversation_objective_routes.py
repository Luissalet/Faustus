"""Thin HTTP layer for a conversation brief and its native associations."""
from fastapi import APIRouter, HTTPException, Request
from routes.session_routes import _verify_session_owner
from src.auth_helpers import effective_user
from src import conversation_objective as objectives


def objective_owner(request, session_id):
    _verify_session_owner(request, session_id)
    return str(effective_user(request) or "")


def objective_error(exc):
    if isinstance(exc, objectives.RevisionConflict):
        raise HTTPException(409, {"reason": "revision_conflict", "current_revision": exc.revision})
    if isinstance(exc, KeyError):
        raise HTTPException(404, "Objective not found")
    if isinstance(exc, ValueError):
        raise HTTPException(400, str(exc))
    raise exc


async def objective_body(request):
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("JSON object required")
        return body
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, str(exc))


def setup_conversation_objective_routes():
    router = APIRouter(prefix="/api/session", tags=["conversation objective"])

    @router.get("/{session_id}/objective")
    def get_objective(request: Request, session_id: str):
        owner = objective_owner(request, session_id)
        value = objectives.get_store().get(owner, session_id)
        from src import agent_runs
        if value:
            for run in value["runs"]:
                run["generation"] = agent_runs.run_status_snapshot(session_id, run["run_id"])
        return {"objective": value, "generation": agent_runs.run_status_snapshot(session_id)}

    @router.put("/{session_id}/objective")
    async def save_objective(request: Request, session_id: str):
        owner = objective_owner(request, session_id)
        body = await objective_body(request)
        try:
            value = objectives.get_store().save(owner, session_id, body.get("expected_revision"), body.get("brief"))
        except (ValueError, KeyError) as exc:
            objective_error(exc)
        return {"objective": value}

    @router.post("/{session_id}/objective/state")
    async def state_objective(request: Request, session_id: str):
        owner = objective_owner(request, session_id)
        body = await objective_body(request)
        from src import agent_runs
        generation = agent_runs.run_status_snapshot(session_id)
        try:
            value = objectives.get_store().transition(owner, session_id, body.get("expected_revision"), body.get("status"), body.get("result", ""))
        except (ValueError, KeyError) as exc:
            objective_error(exc)
        pause_requested = False
        if value["status"] == "paused" and generation and generation.get("active"):
            pause_requested = agent_runs.request_pause(session_id, generation["run_id"])
        return {"objective": value, "pause_requested": pause_requested,
                "generation": generation, "schedule_changed": False}

    return router
