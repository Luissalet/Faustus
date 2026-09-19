"""Trajectory gate — the HTTP door onto `src.trajectory_gate` (declarative,
CI-style assertions over one recorded agent run).

GET /api/agent-runs/{run_id}/gate   — evaluate with the default spec
POST /api/agent-runs/{run_id}/gate  — evaluate with the spec in the body

Auth mirrors `routes/observability_routes.py`'s own session-scoped surface:
`run_id` (a session id, or the opaque per-turn id it minted — see
`trajectory_gate.load_trajectory`'s docstring) is resolved to the session
that owns its log, and the caller must own that session (`_verify_session_
owner`, same helper `routes/chat_routes.py`'s own debugging endpoints use).
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Body, HTTPException, Request

from routes.session_routes import _verify_session_owner


def setup_trajectory_gate_routes() -> APIRouter:
    router = APIRouter(prefix="/api/agent-runs", tags=["trajectory-gate"])

    def _owned_trajectory(request: Request, run_id: str):
        from src import trajectory_gate

        session_id = trajectory_gate._resolve_session_id(run_id)
        if not session_id:
            raise HTTPException(404, f"Run {run_id} not found")
        _verify_session_owner(request, session_id)
        try:
            return trajectory_gate.load_trajectory(run_id)
        except LookupError:
            raise HTTPException(404, f"Run {run_id} not found")

    @router.get("/{run_id}/gate")
    async def gate_default(run_id: str, request: Request):
        from src import trajectory_gate

        traj = _owned_trajectory(request, run_id)
        return trajectory_gate.evaluate(traj, trajectory_gate.default_spec())

    @router.post("/{run_id}/gate")
    async def gate_with_spec(run_id: str, request: Request, spec: Dict[str, Any] = Body(default={})):
        from src import trajectory_gate

        traj = _owned_trajectory(request, run_id)
        return trajectory_gate.evaluate(traj, spec or trajectory_gate.default_spec())

    return router
