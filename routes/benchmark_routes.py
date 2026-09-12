"""routes/benchmark_routes.py — INF-04 A4: the local-inference benchmark
API, registered in `app.py` next to `board_routes` (CONTRATO_INF04's own
instruction — a new router, not folded into `routes/cookbook_routes.py`,
same reasoning `routes/inference_routes.py` already gave for INF-02).

```
GET  /api/bench/suites                          -> {suites}
GET  /api/bench/profiles?endpoint=&model=        -> {current, saved}
POST /api/bench/plan                             -> {run}
POST /api/bench/runs/{id}/start                  -> {run}   (202, background)
GET  /api/bench/runs?limit=                      -> {runs}
GET  /api/bench/runs/{id}                        -> {run}
POST /api/bench/runs/{id}/cancel                 -> {run}
GET  /api/bench/compare?baseline=&candidate=     -> {comparison}
POST /api/bench/profiles/{id}/promote            -> {profile}
```

Owner-scoped the same way INF-02's serve routes are: every endpoint calls
`require_admin` (Cookbook's own gate — this reads/spends compute the same
way a launch does, not the caller's own private data), and `plan()`/the
admission gate inside `start()` get the caller's identity for their own
audit trail, never as a per-owner visibility filter (matching Cookbook:
`list_runs`/`list_profiles` show every run/profile to any admin, exactly
like `GET /api/model/serve/{id}/receipt` is not scoped to who launched it).

Errors are flat `{"error", "error_class"}` bodies — `routes/inference_routes.py`'s
own convention — never a bare `HTTPException`, so `error_class` never gets
nested under FastAPI's default `"detail"` key:

  bench.not_found        — no such suite/run/profile/comparison target.
  bench.bad_state        — `start()` on a run that is not `"planned"`.
  bench.not_comparable   — `promote()` asked for on an incomparable/refused comparison.
  bench.budget_required  — `plan()` without a budget object.
  bench.invalid_plan     — a contract rejection (bad objective, malformed profile).

`POST /runs/{id}/start` is the one place this router starts work that
outlives the request: it hands `runner.start(run_id)` to
`asyncio.create_task` and returns 202 immediately with the run still
`"planned"` in the response body — the caller is expected to poll
`GET /runs/{id}` (or the eventual SSE/websocket progress channel Lote B
adds), never to block on this call. The task is kept in a module-level set
so the event loop cannot garbage-collect it mid-flight (the well-known
"Task was destroyed but it is pending" failure mode for a bare
`create_task()` with no held reference).

`promote`'s request only accepts `candidate_run_id` + `baseline_run_id`
(never a bare `comparison_id`): a `Comparison` is a PURE, derived value in
this lote (`runner.compare()` never persists one with an id of its own,
unlike a `BenchmarkRun`), so there is nothing a `comparison_id` could look
up — and always recomputing the comparison fresh, rather than trusting a
client-supplied verdict, is exactly `src/capability_promotion.py`'s
"evidencia vigente" discipline `src/bench/profiles.py::promote` already
documents borrowing.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from core.middleware import require_admin
from src.auth_helpers import get_current_user
from src.bench import profiles, runner, suites
from src.contracts.base import ContractError
from src.contracts.inference import InferenceProfile, PROFILE_OBJECTIVES

logger = logging.getLogger(__name__)


def _error(status: int, message: str, error_class: str, **extra: Any) -> JSONResponse:
    body: Dict[str, Any] = {"error": message, "error_class": error_class}
    body.update(extra)
    return JSONResponse(status_code=status, content=body)


class PlanRequest(BaseModel):
    endpoint_url: str
    model: str
    suite_id: str
    budget: dict = {}
    objective: str
    options: dict = {}
    hardware_id: Optional[str] = None
    label: Optional[str] = None


class PromoteRequest(BaseModel):
    candidate_run_id: Optional[str] = None
    baseline_run_id: Optional[str] = None


#: Fire-and-forget tasks started by POST /runs/{id}/start. Held here (not
#: just passed to `asyncio.create_task` and dropped) so the event loop
#: cannot garbage-collect a running benchmark mid-case — `add_done_callback`
#: discards the entry once the task actually finishes, success or failure.
_background_tasks: set = set()


def _run_in_background(run_id: str) -> None:
    async def _drive() -> None:
        try:
            await runner.start(run_id)
        except Exception:  # noqa: BLE001 - a background task's own errors must not escape unlogged
            logger.exception("benchmark run %s failed in its background task", run_id)

    task = asyncio.create_task(_drive())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def setup_benchmark_routes() -> APIRouter:
    router = APIRouter(tags=["bench"])

    @router.get("/api/bench/suites")
    async def list_suites_route(request: Request):
        require_admin(request)
        found = suites.list_suites()
        return {
            "suites": [
                {
                    "id": s["id"], "version": s["version"], "objective": s["objective"],
                    "cases": [c.to_dict() for c in s["cases"]],
                }
                for s in found
            ]
        }

    @router.get("/api/bench/profiles")
    async def get_profiles(request: Request, endpoint: str = "", model: str = ""):
        require_admin(request)
        owner = get_current_user(request) or ""
        current = None
        if endpoint and model:
            try:
                current = profiles.current_profile(endpoint, model, owner).to_dict()
            except ContractError as e:
                return _error(400, str(e), "bench.invalid_plan")
        return {"current": current, "saved": [p.to_dict() for p in profiles.list_profiles()]}

    @router.post("/api/bench/plan")
    async def plan_route(request: Request, req: PlanRequest):
        # §01/§09: opening this screen never got this far without a person
        # explicitly submitting a plan request — this endpoint itself still
        # runs nothing, it only builds and persists the PLAN.
        require_admin(request)
        owner = get_current_user(request) or ""
        if req.objective not in PROFILE_OBJECTIVES:
            return _error(400, f"objective must be one of {list(PROFILE_OBJECTIVES)}", "bench.invalid_plan")
        if not req.budget:
            return _error(
                400, "a budget (max_cases, max_seconds, max_generated_tokens and/or repeats) is required",
                "bench.budget_required",
            )
        try:
            profile = profiles.current_profile(req.endpoint_url, req.model, owner, objective=req.objective)
            overrides_applied = bool(req.options or req.hardware_id or req.label)
            if overrides_applied:
                raw = profile.to_dict()
                if req.options:
                    raw["options"] = {**raw["options"], **req.options}
                if req.hardware_id:
                    raw["hardware_id"] = req.hardware_id
                if req.label:
                    raw["label"] = req.label
                if req.options or req.hardware_id:
                    # The fingerprint is a function of (model, engine, options,
                    # hardware_id) — changing any of those without clearing it
                    # would leave a stale fingerprint on a profile that no
                    # longer matches it. `label` alone does not affect identity.
                    raw["fingerprint"] = None
                profile = InferenceProfile.parse(raw)
        except ContractError as e:
            return _error(400, str(e), "bench.invalid_plan")
        try:
            run = runner.plan(profile, req.suite_id, req.budget, owner)
            # A profile a run was planned against must be addressable later:
            # `POST /profiles/{id}/promote` looks it up by id, and the Studio
            # sends the candidate run's own profile id. Saving is not
            # activating (§09): nothing reads this profile into the chat.
            profiles.save_profile(run.profile)
        except suites.SuiteNotFound:
            return _error(404, f"no such suite: {req.suite_id!r}", "bench.not_found")
        except ContractError as e:
            return _error(400, str(e), "bench.invalid_plan")
        return {"run": run.to_dict()}

    @router.post("/api/bench/runs/{run_id}/start")
    async def start_route(request: Request, run_id: str):
        require_admin(request)
        run = runner.get(run_id)
        if run is None:
            return _error(404, f"no such run: {run_id!r}", "bench.not_found")
        if run.state != "planned":
            return _error(409, f"run is in state {run.state!r}, must be 'planned' to start", "bench.bad_state")
        _run_in_background(run_id)
        return JSONResponse(status_code=202, content={"run": run.to_dict()})

    @router.get("/api/bench/runs")
    async def list_runs_route(request: Request, limit: int = 50):
        require_admin(request)
        capped = max(1, min(int(limit), 200))
        return {"runs": [r.to_dict() for r in runner.list_runs(limit=capped)]}

    @router.get("/api/bench/runs/{run_id}")
    async def get_run_route(request: Request, run_id: str):
        require_admin(request)
        run = runner.get(run_id)
        if run is None:
            return _error(404, f"no such run: {run_id!r}", "bench.not_found")
        return {"run": run.to_dict()}

    @router.post("/api/bench/runs/{run_id}/cancel")
    async def cancel_route(request: Request, run_id: str):
        require_admin(request)
        try:
            run = runner.cancel(run_id)
        except runner.RunNotFound:
            return _error(404, f"no such run: {run_id!r}", "bench.not_found")
        return {"run": run.to_dict()}

    @router.get("/api/bench/compare")
    async def compare_route(request: Request, baseline: str, candidate: str):
        require_admin(request)
        try:
            comparison = runner.compare(baseline, candidate)
        except runner.RunNotFound as e:
            return _error(404, f"no such run: {e}", "bench.not_found")
        return {"comparison": comparison.to_dict()}

    @router.post("/api/bench/profiles/{profile_id}/promote")
    async def promote_route(request: Request, profile_id: str, req: PromoteRequest):
        require_admin(request)
        if not req.candidate_run_id or not req.baseline_run_id:
            # The closed error vocabulary (CONTRATO_INF04 A4) has no
            # "missing field" class of its own; `bench.not_comparable` is the
            # nearest fit — there is nothing to compare without both ids, so
            # there is nothing this call could ever promote.
            return _error(
                400, "candidate_run_id and baseline_run_id are both required", "bench.not_comparable",
            )
        try:
            comparison = runner.compare(req.baseline_run_id, req.candidate_run_id)
        except runner.RunNotFound as e:
            return _error(404, f"no such run: {e}", "bench.not_found")
        try:
            profile = profiles.promote(profile_id, comparison)
        except profiles.ProfileNotFound:
            return _error(404, f"no such profile: {profile_id!r}", "bench.not_found")
        except profiles.PromotionRefused as e:
            return _error(409, str(e), "bench.not_comparable")
        return {"profile": profile.to_dict()}

    return router
