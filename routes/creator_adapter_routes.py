"""Creator adapter routes (WP10), over ``src.creator.adapter_port`` /
``src.creator.adapters`` / ``src.creator.production_runs``.

Same discipline as every other ``routes/creator_*`` module: gated by the
``creator_enabled`` setting (default False), checked before any store
access; owner always resolved from the authenticated session, never the
body (CONTRATO.md rule 3).

Three routes:

* ``GET /api/creator/adapters`` — every registered adapter's manifest
  (``describe()`` — a query, may probe the engine, never queues anything).
* ``POST /api/creator/adapters/{name}/plan`` — ``plan()`` (pure).
* ``POST /api/creator/adapters/{name}/submit`` — the one EFFECT route.
  Recomputes the WP09 preflight for this exact operation, and:

  - if it does not require approval (budget verdict ``allow``), proceeds;
  - if it does, the caller's ``preflight_digest`` must match the freshly
    recomputed digest (409 ``digest_mismatch`` otherwise — the plan
    changed since the caller last saw it, same as
    ``routes/creator_preflight_routes.py``'s own ``/approve``), a GRANTED
    approval covering that exact plan must exist (403 if not — "submit
    sin aprobación"), and it is spent through
    ``approval_store.consume()`` — a SINGLE use. Spending it twice answers
    409, because the second call finds a card with ``uses_left`` already
    at 0 / a status that is no longer ``granted``.

  Only then are the request's occurrence ids staged (``adapters.base.
  stage_inputs`` — the only place an id becomes a path) and handed to the
  adapter's ``submit()``, and the result projected onto ``MediaRun`` via
  ``production_runs.record_submit()``.
"""
from __future__ import annotations

import os
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request

from src.auth_helpers import require_user


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


def _get_adapter(name: str):
    from src.creator import adapters
    try:
        return adapters.get(name)
    except KeyError as exc:
        raise HTTPException(404, str(exc))


def _parse_op_body(payload: Dict[str, Any]) -> Dict[str, Any]:
    project_id = str(payload.get("project_id") or "")
    op = str(payload.get("op") or "")
    params = payload.get("params")
    if params is not None and not isinstance(params, dict):
        raise HTTPException(400, "params must be an object")
    inputs = payload.get("inputs") or []
    if not isinstance(inputs, list):
        raise HTTPException(400, "inputs must be a list")
    if not project_id:
        raise HTTPException(400, "project_id is required")
    if not op:
        raise HTTPException(400, "op is required")
    return {"project_id": project_id, "op": op, "params": dict(params or {}),
            "inputs": [str(i) for i in inputs]}


def setup_creator_adapter_routes() -> APIRouter:
    router = APIRouter(prefix="/api/creator/adapters", tags=["creator-adapters"])

    @router.get("")
    def list_adapters(request: Request):
        _owner(request)
        _require_flag()
        from src.creator import adapters
        manifests = []
        for name, factory in sorted(adapters.registry().items()):
            try:
                manifests.append(factory().describe().to_dict())
            except Exception as exc:  # noqa: BLE001 — one broken adapter must not blank the list
                manifests.append({"name": name, "available": False,
                                   "reason": f"describe() raised: {exc}"})
        return {"adapters": manifests}

    @router.post("/{name}/plan")
    async def plan_adapter(request: Request, name: str):
        _owner(request)
        _require_flag()
        adapter = _get_adapter(name)
        payload = await _json_object(request)
        body = _parse_op_body(payload)
        plan = adapter.plan(body["op"], body["params"], body["inputs"])
        return plan.to_dict()

    @router.post("/{name}/submit")
    async def submit_adapter(request: Request, name: str):
        owner = _owner(request)
        _require_flag()
        adapter = _get_adapter(name)
        payload = await _json_object(request)
        body = _parse_op_body(payload)
        engine = str(payload.get("engine") or name)
        deployment_id = str(payload.get("deployment_id") or name)
        preflight_digest = str(payload.get("preflight_digest") or "")

        from src.creator import profile as profile_mod
        from src.creator.preflight import run_preflight
        from services.projects import get_store as get_project_store

        if get_project_store().get(body["project_id"], owner) is None:
            raise HTTPException(404, "project not found")

        profile = profile_mod.get_profile(get_project_store(), owner, body["project_id"]) or {}
        report = run_preflight(
            owner=owner, project_id=body["project_id"], operation=body["op"],
            engine=engine, deployment_id=deployment_id, params=body["params"],
            inputs=body["inputs"], profile=profile,
        )

        if report.requires_approval:
            if not preflight_digest:
                raise HTTPException(403, {
                    "reason": "approval_required",
                    "detail": "this operation needs approval; call "
                              "POST /api/creator/preflight then "
                              "/api/creator/preflight/{digest}/approve first",
                    "digest": report.approval_digest,
                })
            if preflight_digest != report.approval_digest:
                raise HTTPException(409, {
                    "reason": "digest_mismatch",
                    "detail": "the plan behind this digest has changed since preflight",
                    "current_digest": report.approval_digest,
                })
            from src import approval_store
            from src.contracts import ApprovalPlan
            from core.database import ApprovalRow, SessionLocal

            plan_obj = ApprovalPlan.parse(report.approval_plan)
            # Looked up by fingerprint regardless of CURRENT status — a card
            # that was granted and has since been spent must still be found
            # here, so a second submit reaches `consume()` (which answers
            # "already spent") rather than a lookup that only sees granted
            # cards and would report the same "not approved" a submit with
            # no approval at all gets. That is exactly the distinction
            # between "submit sin aprobación → 403" and "aprobación
            # consumida dos veces → 409".
            db = SessionLocal()
            try:
                row = (db.query(ApprovalRow)
                       .filter(ApprovalRow.plan_fingerprint == plan_obj.fingerprint(),
                               ApprovalRow.owner == (owner or None))
                       .order_by(ApprovalRow.decided_at.desc()).first())
                approval_id = row.id if row is not None else ""
            finally:
                db.close()
            if not approval_id:
                raise HTTPException(403, {
                    "reason": "not_approved",
                    "detail": "no approval was ever granted for this exact plan; approve "
                              f"/api/creator/preflight/{preflight_digest}/approve first",
                })
            spent = approval_store.consume(approval_id, plan_obj, owner=owner)
            if not spent.get("ok"):
                raise HTTPException(409, {
                    "reason": spent.get("reason", "consume_failed"),
                    "detail": "this approval has already been spent, or the plan "
                              "changed since it was granted",
                })
        elif not report.ok:
            raise HTTPException(400, {"reason": "not_ready", "missing": list(report.missing)})

        from src.creator.adapters.base import stage_inputs

        staging = stage_inputs(owner=owner, project_id=body["project_id"],
                                occurrence_ids=body["inputs"])
        params = dict(body["params"])
        for key, value in list(params.items()):
            staged_path = staging.input_paths.get(str(value))
            if staged_path is not None:
                params[key] = staged_path

        plan = adapter.plan(body["op"], params, body["inputs"])
        submitted = adapter.submit(plan, staging)

        from src.creator import production_runs

        projected = production_runs.record_submit(
            adapter_name=name, engine=engine, op=body["op"], job_id=submitted.job_id,
            submit_state=submitted.state, owner=owner, project_id=body["project_id"],
            values=body["params"], reason=submitted.reason or submitted.detail,
        )
        # A synchronous adapter (ffmpeg today) is already terminal the
        # moment submit() returns; an asynchronous one (ComfyUI) is not, and
        # already has its own MediaRun row mid-flight from `media_runs.
        # start()` above (`production_runs.record_submit()` read it back,
        # never wrote it) — collecting here would be a no-op for it, so this
        # only actually does anything for a non-`media_runs` adapter.
        if name != "comfyui" and submitted.job_id:
            status = adapter.status(submitted.job_id)
            if status.state == "completed":
                tmp = os.path.join(staging.workdir, "collect")
                collected = adapter.collect(submitted.job_id, tmp)
                projected = production_runs.link_outputs(
                    submitted.job_id, adapter_name=name, collected=collected,
                    owner=owner, project_id=body["project_id"], op=body["op"],
                    input_occurrence_ids=body["inputs"],
                )
            elif status.state == "failed":
                projected = production_runs.mark_terminal(
                    submitted.job_id, adapter_name=name, status="failed",
                    reason=status.detail)

        return {"submit": submitted.to_dict(), "run": projected}

    return router
