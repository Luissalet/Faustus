"""Multimodal capabilities and parameter contracts — WP07 (MOD-03..06,
MOD-08, MOD-20, QA06), over `src/creator/capabilities.py` and
`src/creator/params.py`.

  GET  /api/creator/capabilities/models             Every deployment
                                                      `ModelIdentityStore`
                                                      already knows about —
                                                      no resolution, no
                                                      model load.
  GET  /api/creator/capabilities/{deployment_id}     Full multimodal
                                                      capability profile
                                                      (known/announced/
                                                      unknown per axis) for
                                                      one deployment.
  GET  /api/creator/params?engine=&task=             ParamSchema for
                                                      (engine, task), shaped
                                                      for the Studio to paint
                                                      controls from.
  POST /api/creator/params/validate                  Validate a params body
                                                      against (engine, task);
                                                      never partial — every
                                                      problem is reported.

Gated on `creator_enabled` (CONTRATO rule 5, default False), checked before
any store access — with the flag off every route below answers 404 and
nothing is read. `require_user` on every route; nothing here writes evidence
(that stays on `routes/model_identity_routes.py`'s admin-only endpoint, WP06)
or mutates a document, so no owner-scoped storage check is needed — this
module is read-only plus pure validation.

NOT YET WIRED into `app.py` (CONTRATO rule 11 / out of this lot's file
ownership) — see `WP07_wiring.md` for the one
`app.include_router(setup_creator_capability_routes())` line.
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Query, Request

from src.auth_helpers import require_user
from src.creator import capabilities as cap
from src.creator import params as pr
from src import model_identity as mi


def _creator_enabled_or_404() -> None:
    from src.settings import get_setting
    if not bool(get_setting("creator_enabled", False)):
        raise HTTPException(status_code=404, detail="creator is not enabled")


def setup_creator_capability_routes() -> APIRouter:
    router = APIRouter(prefix="/api/creator", tags=["creator-capabilities"])

    @router.get("/capabilities/models")
    async def list_known_models(request: Request) -> Dict[str, Any]:
        require_user(request)
        _creator_enabled_or_404()
        store = mi.default_store()
        rows = cap.known_deployments_summary(store=store)
        return {"deployments": list(rows)}

    @router.get("/capabilities/{deployment_id}")
    async def get_capability_profile(deployment_id: str, request: Request) -> Dict[str, Any]:
        require_user(request)
        _creator_enabled_or_404()
        store = mi.default_store()
        deployment = store.get_deployment(deployment_id)
        if deployment is None:
            raise HTTPException(status_code=404, detail="unknown deployment_id")
        profile = cap.capability_profile_for_deployment(deployment_id, store=store)
        return profile.to_dict()

    @router.get("/params")
    async def get_param_schema(
        request: Request,
        engine: str = Query(..., description="Engine id, e.g. 'ace_step', 'invoke', 'wan'"),
        task: str = Query(..., description="Task id, e.g. 'image.edit', 'music.generate'"),
    ) -> Dict[str, Any]:
        require_user(request)
        _creator_enabled_or_404()
        schema = pr.describe_for_ui(engine, task)
        if schema is None:
            raise HTTPException(status_code=404, detail=f"no param schema for engine={engine!r} task={task!r}")
        return {"schema": schema}

    @router.post("/params/validate")
    async def validate_params(request: Request) -> Dict[str, Any]:
        require_user(request)
        _creator_enabled_or_404()
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        body = body if isinstance(body, dict) else {}
        engine = str(body.get("engine") or "").strip()
        task = str(body.get("task") or "").strip()
        params = body.get("params") if isinstance(body.get("params"), dict) else {}
        if not engine or not task:
            raise HTTPException(status_code=400, detail="engine and task are required")
        result = pr.validate(engine, task, params)
        return result.to_dict()

    return router
