"""Model Explorer — WP08 (UX11, MOD07, MOD10, MOD11, MOD12, MOD16, MOD17),
over `src/creator/model_explorer.py`.

  GET  /api/creator/models/explorer                Every known deployment,
                                                     compact rows for the
                                                     Studio's list/filter
                                                     view — no per-row
                                                     footprint/legacy lookup.
  GET  /api/creator/models/explorer/{deployment_id} The full ficha for one
                                                     deployment: ModelSpec +
                                                     DeploymentManifest +
                                                     capability profile +
                                                     param schemas + VRAM
                                                     footprint + legacy
                                                     calibration. 404 for a
                                                     deployment this install
                                                     has never resolved.
  POST /api/creator/models/compare                  {deployment_ids: [..2-3],
                                                     task: "image.edit"} ->
                                                     a cell-by-cell diff
                                                     table. Never invents:
                                                     every cell not backed by
                                                     real evidence is
                                                     "unknown".

Gated on `creator_enabled` (CONTRATO rule 5, default False), checked before
any store access. `require_user` on every route; nothing here writes
anything (read-only aggregation + pure comparison), so no owner-scoped
storage check applies — deployments/capability evidence are install-wide
facts, the same posture `routes/creator_capability_routes.py` already takes.

Wired into `app.py` by this lot (file-ownership table, WP08/C4): one
`app.include_router(setup_creator_model_explorer_routes())` line, same spot
as the other `creator_*` routers.
"""
from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Request

from src.auth_helpers import require_user
from src.creator import model_explorer as me
from src import model_identity as mi


def _creator_enabled_or_404() -> None:
    from src.settings import get_setting
    if not bool(get_setting("creator_enabled", False)):
        raise HTTPException(status_code=404, detail="creator is not enabled")


_MAX_COMPARE = 3
_MIN_COMPARE = 2


def setup_creator_model_explorer_routes() -> APIRouter:
    router = APIRouter(prefix="/api/creator", tags=["creator-model-explorer"])

    @router.get("/models/explorer")
    async def list_explorer(request: Request) -> Dict[str, Any]:
        require_user(request)
        _creator_enabled_or_404()
        store = mi.default_store()
        rows = me.list_entries(store=store)
        return {"deployments": rows}

    @router.get("/models/explorer/{deployment_id}")
    async def get_explorer_entry(deployment_id: str, request: Request) -> Dict[str, Any]:
        require_user(request)
        _creator_enabled_or_404()
        store = mi.default_store()
        entry = me.entry_for_deployment(deployment_id, store=store)
        if entry is None:
            raise HTTPException(status_code=404, detail="unknown deployment_id")
        return entry.to_dict()

    @router.post("/models/compare")
    async def compare_deployments(request: Request) -> Dict[str, Any]:
        require_user(request)
        _creator_enabled_or_404()
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        body = body if isinstance(body, dict) else {}
        deployment_ids = body.get("deployment_ids")
        task = str(body.get("task") or "").strip()
        if not isinstance(deployment_ids, list) or not all(isinstance(d, str) for d in deployment_ids):
            raise HTTPException(status_code=400, detail="deployment_ids must be a list of strings")
        ids: List[str] = [d.strip() for d in deployment_ids if d.strip()]
        if not (_MIN_COMPARE <= len(ids) <= _MAX_COMPARE):
            raise HTTPException(status_code=400, detail=f"deployment_ids must have {_MIN_COMPARE}-{_MAX_COMPARE} entries")
        if not task:
            raise HTTPException(status_code=400, detail="task is required")

        store = mi.default_store()
        table = me.compare(ids, task, store=store)
        return table.to_dict()

    return router
