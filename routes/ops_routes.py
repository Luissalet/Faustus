"""routes/ops_routes.py — OPS-07 (remote cost) and EVAL-03 (chaos dry-run).

Two small admin surfaces, neither with a UI yet (a future lot's job):

``GET /api/ops/remote-cost`` is the HTTP door onto
``src.cleanup_service.remote_cost_report`` — sums the measured
``external_worker_result`` cost events ``src.external_worker.run_task`` now
persists to ``DATA_DIR/runs/*.jsonl``. Admin-gated like the rest of the
instance-wide operational surface (``routes/observability_routes.py``):
this is spend across every user's remote runs, not one session's own data.

``POST /api/ops/chaos/{fixture}`` is dry-run only — it returns what
``src.chaos`` says the named fixture would inject and the result the real
mechanism guarantees, never actually injecting anything. Admin-gated because
naming these mechanisms in detail is itself operational information.

``POST /api/ops/security-probes`` runs ``src.security_probes``'s
deterministic prompt-injection probe catalogue (never ``live`` — that mode
needs a real model and is CLI-only, see ``src/security_probes.py``) and
returns the summary dict. No real tool is ever dispatched by either mode:
deterministic mode only asks the same gate ``execute_tool_block`` asks
before running anything. Admin-gated like the fixtures above.

``GET /api/ops/admission`` (ADP-32) is the read-only status door onto
``src.resource_admission`` — every explicit pool defined so far, with its
live in-use/foreground-waiting counters. Admin-gated like the rest of this
module: pool membership names internal endpoints. Nothing here is wired to
``src.llm_core``'s global local-model lock yet — see
``docs/api/resource_admission.md`` for how it would connect.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from core.middleware import require_admin


def setup_ops_routes():
    router = APIRouter(prefix="/api/ops")

    @router.get("/remote-cost")
    async def remote_cost(request: Request, since: float = None):
        require_admin(request)
        from src.cleanup_service import remote_cost_report
        return remote_cost_report(since=since)

    @router.post("/chaos/{fixture}")
    async def chaos_fixture(fixture: str, request: Request):
        require_admin(request)
        from src import chaos
        try:
            return chaos.dry_run(fixture)
        except KeyError:
            raise HTTPException(
                404,
                f"no such chaos fixture: {fixture!r}; available: {sorted(chaos.FIXTURES)}",
            )

    @router.post("/security-probes")
    async def security_probes(request: Request):
        require_admin(request)
        from src import security_probes
        return security_probes.run_catalogue_deterministic()

    @router.get("/admission")
    async def admission_status(request: Request):
        require_admin(request)
        from src import resource_admission
        return resource_admission.status()

    return router
