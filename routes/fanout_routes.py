"""routes/fanout_routes.py — R3 (Reach wave): `/api/fanout/*`.

Gating follows `routes/alternatives_routes.py`'s own split: starting a run
and applying a winner are `require_human` (an isolated worktree per
candidate, a worker running inside it, and -- on apply -- a write into the
user's main copy); status/results are `require_user` (this owner's own
data, read-only).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core.middleware import require_human
from src.auth_helpers import effective_user, require_user
from src.fanout import runner as _runner
from src.fanout import service as _service
from src.fanout.plan import FanoutCandidate, FanoutPlan

logger = logging.getLogger(__name__)


class CandidateRequest(BaseModel):
    label: str = Field(..., min_length=1, max_length=200)
    model: str = Field("", max_length=400)
    endpoint_url: str = Field("", max_length=2000)
    endpoint_id: str = Field("", max_length=200)
    profile: str = Field("", max_length=200)


class StartFanoutRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=20000)
    workspace: str = Field(..., min_length=1, max_length=4000)
    candidates: List[CandidateRequest] = Field(default_factory=list)
    max_rounds: int = Field(8, ge=1, le=50)
    budget_tokens: int = Field(0, ge=0)
    project_id: str = Field("", max_length=200)
    goal: str = Field("", max_length=2000)


class ApplyRequest(BaseModel):
    confirm: bool = False


def setup_fanout_routes() -> APIRouter:
    router = APIRouter(prefix="/api/fanout", tags=["fanout"])

    def _error(status: int, error_class: str, detail: Optional[str] = None, **extra: Any) -> JSONResponse:
        body: Dict[str, Any] = {"error_class": error_class}
        if detail is not None:
            body["detail"] = detail
        body.update(extra)
        return JSONResponse(status_code=status, content=body)

    def _not_found(exc: _runner.FanoutNotFoundError) -> JSONResponse:
        return _error(404, "fanout.not_found", str(exc))

    @router.post("")
    def start_fanout(body: StartFanoutRequest, request: Request,
                      _h: None = Depends(require_human)) -> Any:
        owner = effective_user(request)
        candidates = [FanoutCandidate.from_dict(c.model_dump()) for c in body.candidates]
        if not candidates:
            from src.fanout.plan import from_settings_default_candidates
            candidates = from_settings_default_candidates(coordinator_model="", coordinator_endpoint_url="")
        plan = FanoutPlan(
            prompt=body.prompt, workspace=body.workspace, candidates=candidates,
            max_rounds=body.max_rounds, budget_tokens=body.budget_tokens,
            project_id=body.project_id, goal=body.goal or body.prompt,
        )
        try:
            run_id = _service.start(owner, plan)
        except ValueError as exc:
            return _error(400, "fanout.invalid_request", str(exc))
        return {"run_id": run_id, "candidates": [c.label for c in candidates]}

    @router.get("/{run_id}")
    def get_status(run_id: str, request: Request, _u: str = Depends(require_user)) -> Any:
        owner = effective_user(request)
        try:
            return _service.status(run_id, owner)
        except _runner.FanoutNotFoundError as exc:
            return _not_found(exc)

    @router.get("/{run_id}/results")
    def get_results(run_id: str, request: Request, _u: str = Depends(require_user)) -> Any:
        owner = effective_user(request)
        try:
            return _service.results(run_id, owner)
        except _runner.FanoutNotFoundError as exc:
            return _not_found(exc)

    @router.get("/{run_id}/diff")
    def get_diff(run_id: str, label_a: str, label_b: str, request: Request,
                 _u: str = Depends(require_user)) -> Any:
        owner = effective_user(request)
        try:
            return _service.diff_between(run_id, owner, label_a, label_b)
        except _runner.FanoutNotFoundError as exc:
            return _not_found(exc)

    @router.post("/{run_id}/apply")
    def apply_winner(run_id: str, label: str, body: ApplyRequest, request: Request,
                      _h: None = Depends(require_human)) -> Any:
        owner = effective_user(request)
        try:
            return _service.apply(run_id, owner, label, confirm=body.confirm)
        except _runner.FanoutNotFoundError as exc:
            return _not_found(exc)
        except Exception as exc:  # noqa: BLE001 - alternatives.ApplyConflictError included
            from src import alternatives as _alternatives
            if isinstance(exc, _alternatives.ApplyConflictError):
                return _error(409, exc.error_class, str(exc), conflicts=exc.conflicts)
            return _error(400, "fanout.apply_failed", str(exc))

    return router
