"""Creator production-plan routes (WP22), over
``src.creator.production_plan`` / ``src.creator.storyboard``.

Same discipline as every other ``routes/creator_*`` module: gated by the
``creator_enabled`` setting (default False), checked before any store
access; owner always resolved from the authenticated session, never the
body (CONTRATO.md rule 3).

Five routes:

* ``POST /api/creator/plans`` — validate + persist a new
  :class:`~src.creator.production_plan.ProductionPlan` (revision 1).
* ``GET /api/creator/plans/{id}`` — read it back.
* ``POST /api/creator/plans/{id}/execute`` — compile to a real workflow and
  run it. ``idempotency_key`` makes a retried call return the SAME
  production. If any step needs approval, a caller with no/stale
  ``approval_digest`` gets 403 back with the current digest to approve
  (same shape as ``routes/creator_adapter_routes.py``'s own
  ``/submit``); a plan whose input documents moved since it was created
  gets 409.
* ``GET /api/creator/productions/{id}`` — the projected status (workflow +
  MediaRun, never a second state machine).
* ``POST /api/creator/productions/{id}/cancel`` — stop what has not started,
  reconcile what has.
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request

from src.auth_helpers import require_user
from src.creator.errors import InvalidOperation


def _owner(request: Request) -> str:
    from core import middleware as mw
    from src.owner_identity import effective_storage_owner
    user = require_user(request)
    return effective_storage_owner(user, auth_is_disabled=mw.auth_disabled()) or ""


def _require_flag() -> None:
    from src.settings import get_setting
    if not bool(get_setting("creator_enabled", False)):
        raise HTTPException(status_code=404, detail="creator is not enabled")


async def _json_object(request: Request) -> Dict[str, Any]:
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "Request body must be JSON")
    if not isinstance(payload, dict):
        raise HTTPException(400, "Request body must be a JSON object")
    return payload


def _require_project(owner: str, project_id: str) -> None:
    from services.projects import get_store as get_project_store
    if not project_id or get_project_store().get(project_id, owner) is None:
        raise HTTPException(404, "project not found")


def _profile_for(owner: str, project_id: str) -> Dict[str, Any]:
    from src.creator import profile as profile_mod
    from services.projects import get_store as get_project_store
    return profile_mod.get_profile(get_project_store(), owner, project_id) or {}


def setup_creator_plan_routes() -> APIRouter:
    router = APIRouter(prefix="/api/creator", tags=["creator"])

    @router.post("/plans")
    async def post_plan(request: Request):
        owner = _owner(request)
        _require_flag()
        payload = await _json_object(request)
        project_id = str(payload.get("project_id") or "")
        _require_project(owner, project_id)

        from src.creator import production_plan

        store = production_plan.get_store()
        try:
            plan = production_plan.create(store, owner, project_id, payload)
        except InvalidOperation as exc:
            raise HTTPException(400, str(exc))
        return plan.to_public_dict()

    @router.get("/plans/{plan_id}")
    async def get_plan(request: Request, plan_id: str):
        owner = _owner(request)
        _require_flag()
        from src.creator import production_plan

        store = production_plan.get_store()
        try:
            plan = production_plan.get(store, owner, plan_id)
        except production_plan.PlanNotFound:
            raise HTTPException(404, "plan not found")
        return plan.to_public_dict()

    @router.post("/plans/{plan_id}/execute")
    async def post_execute(request: Request, plan_id: str):
        owner = _owner(request)
        _require_flag()
        payload = await _json_object(request)
        idempotency_key = str(payload.get("idempotency_key") or "")
        approval_digest = payload.get("approval_digest")
        if approval_digest is not None and not isinstance(approval_digest, str):
            raise HTTPException(400, "approval_digest must be a string")
        if not idempotency_key:
            raise HTTPException(400, "idempotency_key is required")

        from src.creator import production_plan

        store = production_plan.get_store()
        try:
            plan = production_plan.get(store, owner, plan_id)
        except production_plan.PlanNotFound:
            raise HTTPException(404, "plan not found")

        profile = _profile_for(owner, plan.project_id)
        try:
            result = production_plan.execute(
                store, plan_id, owner=owner, idempotency_key=idempotency_key,
                approval_digest=approval_digest, profile=profile,
            )
        except production_plan.PlanRevisionConflict as exc:
            raise HTTPException(409, {
                "reason": "input_documents_changed",
                "detail": "one or more input documents changed since this plan was created",
                "changed": exc.changed,
            })
        except InvalidOperation as exc:
            est = production_plan.estimate(plan, profile=profile)
            raise HTTPException(403, {
                "reason": "approval_required", "detail": str(exc),
                "approval_digest": est.get("approval_digest"),
                "approval_plan": est.get("approval_plan"),
                "missing": est.get("missing"),
            })
        return result

    @router.get("/productions/{production_id}")
    async def get_production(request: Request, production_id: str):
        owner = _owner(request)
        _require_flag()
        from src.creator import production_plan

        store = production_plan.get_store()
        try:
            return production_plan.production_status(store, production_id, owner=owner)
        except production_plan.ProductionNotFound:
            raise HTTPException(404, "production not found")

    @router.post("/productions/{production_id}/cancel")
    async def post_cancel(request: Request, production_id: str):
        owner = _owner(request)
        _require_flag()
        from src.creator import production_plan

        store = production_plan.get_store()
        try:
            return production_plan.cancel(store, production_id, owner=owner)
        except production_plan.ProductionNotFound:
            raise HTTPException(404, "production not found")

    return router
