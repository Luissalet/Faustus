# routes/execution_routes.py
"""`/api/sandbox` — what the execution sandbox really confines.

* ``GET  /api/sandbox``        the configured policy and the documented
  coverage per operation on this host (no Docker call).
* ``POST /api/sandbox/probe``  runs the real allowed/forbidden checks
  (``src/sandbox_probe.py``) and returns the per-operation report. It starts
  containers and a throw-away local server, so it is an admin action.

`/api/process-handles` lists the process handles of ``src/process_manager.py``
(admin read) and stops one by handle. Stop is ``require_human`` like the
process center's: it is the user's control, not something a tool token may
use, and it only ever signals the tree the manager started.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from core.middleware import require_admin, require_human


class ProbeBody(BaseModel):
    operations: Optional[List[str]] = None


class StopBody(BaseModel):
    handle: str
    session_id: str = ""
    reason: str = ""


def setup_execution_routes() -> APIRouter:
    router = APIRouter(tags=["sandbox"])

    @router.get("/api/sandbox")
    async def policy(request: Request) -> Dict[str, Any]:
        require_admin(request)
        from src import sandbox_exec, sandbox_probe
        from src.code_mode import confined
        return {
            "sandbox": sandbox_exec.describe(),
            "code_mode": {"runtime": confined.resolve_runtime(),
                          "workspace_access": confined.resolve_workspace_access(),
                          "network": confined.resolve_network()},
            "host": sandbox_probe._host_kind(),
            "coverage": sandbox_probe.coverage_for_host(),
        }

    @router.post("/api/sandbox/probe")
    async def probe(body: ProbeBody, request: Request) -> Dict[str, Any]:
        require_admin(request)
        from src import sandbox_probe
        report = await sandbox_probe.run_sandbox_probe(body.operations)
        report["rendered"] = sandbox_probe.render_text(report)
        return report

    @router.get("/api/process-handles")
    async def handles(request: Request, session_id: str = "") -> Dict[str, Any]:
        require_admin(request)
        import asyncio
        from src.process_manager import Caller, manager
        return await asyncio.to_thread(manager().list, Caller(session_id=session_id))

    @router.post("/api/process-handles/stop")
    async def stop(body: StopBody, request: Request) -> Dict[str, Any]:
        require_human(request)
        import asyncio
        from src.process_manager import Caller, manager
        result = await asyncio.to_thread(
            manager().stop, body.handle, Caller(session_id=body.session_id), reason=body.reason)
        if not result.get("ok") and result.get("code") in ("not_owner", "unknown_handle"):
            raise HTTPException(404, result.get("error") or "no such handle")
        return result

    return router
