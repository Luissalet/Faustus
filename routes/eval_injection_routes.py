"""routes/eval_injection_routes.py - the prompt-injection battery over HTTP (Agora #206).

``POST /api/evals/injection/run`` starts an async job (admin only) and returns its id
at once; poll ``GET /api/evals/injection/jobs/{job_id}`` and read the result from
``GET /api/evals/injection/report``. The default run is deterministic: a scripted
model, no network, nothing dispatched. ``{"live": true, ...}`` additionally needs
``base_url`` (a running Faustus), ``endpoint_url`` and ``model``; it is off unless asked.
See docs/evals/injection.md.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

from core.middleware import require_admin


def setup_eval_injection_routes():
    router = APIRouter(prefix="/api/evals/injection")

    @router.get("/cases")
    async def list_cases(request: Request):
        require_admin(request)
        from evals.injection.cases import CASES, HOLES
        return {
            "cases": [{"id": c.id, "title": c.title, "vector": c.vector, "technique": c.technique,
                       "user_task": c.user_task, "forbidden": [f.what or f.tool for f in c.forbidden],
                       "known_hole": c.known_hole} for c in list(CASES) + list(HOLES)],
        }

    @router.post("/run")
    async def run(request: Request):
        require_admin(request)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - an empty body is a default run
            body = {}
        if not isinstance(body, dict):
            raise HTTPException(400, "body must be a JSON object")
        from evals.injection import service
        try:
            return service.start_job(body)
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @router.get("/jobs/{job_id}")
    async def job(job_id: str, request: Request):
        require_admin(request)
        from evals.injection import service
        found = service.get_job(job_id)
        if found is None:
            raise HTTPException(404, f"no such injection eval job: {job_id}")
        return found

    @router.get("/report")
    async def report(request: Request, job_id: str = "", mode: str = "", format: str = "json"):
        require_admin(request)
        from evals.injection import runner, service
        summary = service.latest_report(job_id=job_id or None, mode=mode or None)
        if summary is None:
            raise HTTPException(404, "no injection eval report yet; POST /api/evals/injection/run first")
        if format == "markdown":
            return PlainTextResponse(runner.markdown_report(summary, transcripts=True))
        return summary

    return router
