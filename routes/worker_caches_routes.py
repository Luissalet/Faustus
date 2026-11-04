"""Worker caches API — every isolated worker's own package caches (Darwin #999).

``src/worker_caches.py`` gives each delegated worker (delegate_agents, the
minions of an agent definition, dispatch and swarm workers) and each fan-out
candidate its own pip/npm/uv/yarn/pnpm/poetry/go/nuget caches and temp folder.
This is where a screen (Agents → Worker caches, the worker card on the
sub-agent board) or the ``workers_caches`` MCP tool sees them.

  GET  /api/agents/worker-caches?session_id=&parent_session_id=&kind=
       → {base, variables, workers: [{root, kind, name, session_id,
          parent_session_id, agent, role, workspace, created, active, bytes,
          files, tools: {pip: bytes, ...}, approximate, cross_reads}],
          total_bytes, active}
  POST /api/agents/worker-caches/clear {root? | session_id? | all?, force?, forget?}
       → {cleared: [{root, name, freed_bytes, forgotten}], skipped, freed_bytes}

Clearing only touches folders the listing knows; a running worker is skipped
unless ``force``.
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from src import worker_caches
from src.auth_helpers import require_user


def setup_worker_caches_routes() -> APIRouter:
    router = APIRouter(prefix="/api/agents", tags=["agents"])

    @router.get("/worker-caches")
    async def list_worker_caches(request: Request, session_id: str = "",
                                 parent_session_id: str = "", kind: str = "") -> Dict[str, Any]:
        require_user(request)
        return await run_in_threadpool(
            worker_caches.list_caches, session_id=session_id.strip(),
            parent_session_id=parent_session_id.strip(), kind=kind.strip())

    @router.post("/worker-caches/clear")
    async def clear_worker_caches(request: Request) -> Dict[str, Any]:
        require_user(request)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - an empty body is a bad request below
            body = {}
        body = body if isinstance(body, dict) else {}
        try:
            return await run_in_threadpool(
                worker_caches.clear,
                root=str(body.get("root") or "").strip(),
                session_id=str(body.get("session_id") or "").strip(),
                all_idle=bool(body.get("all")),
                force=bool(body.get("force")),
                forget=bool(body.get("forget")),
            )
        except worker_caches.WorkerCacheError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return router
