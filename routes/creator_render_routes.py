"""Creator render routes (WP14) — timeline -> deterministic FFmpeg output.

Same discipline as every other ``routes/creator_*`` module: gated by the
``creator_enabled`` setting (checked before any store access), owner always
resolved from the authenticated session, never the body (CONTRATO.md rule
3). The approval flow mirrors ``routes/creator_adapter_routes.py``'s
``/adapters/{name}/submit`` EXACTLY (same preflight -> digest match ->
granted-approval lookup -> ``approval_store.consume()`` sequence) — a render
is, underneath, one more Creator operation that can cost real compute, and
the ficha requires it: "POST /api/creator/render exige preflight_digest
aprobado de WP09".

Four routes:

* ``POST /api/creator/render/plan`` — pure: ``render.service.plan()``, no
  approval needed to just SEE the plan/estimate/cache status.
* ``POST /api/creator/render`` — the effect. Approval-gated as above, then
  ``render.service.start_render()`` (async — returns a ``render_token``
  immediately; the actual ffmpeg run continues on a background thread).
* ``GET /api/creator/render/{job_id}`` — ``job_id`` is the ``render_token``
  ``start_render()`` returned; ``render.service.get_job()``.
* ``POST /api/creator/render/{job_id}/cancel`` — ``render.service.cancel_job()``.
"""
from __future__ import annotations

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


def _parse_render_body(payload: Dict[str, Any]) -> Dict[str, Any]:
    project_id = str(payload.get("project_id") or "")
    doc_id = str(payload.get("doc_id") or "")
    profile_id = str(payload.get("profile_id") or "")
    subtitle_occurrence_id = payload.get("subtitle_occurrence_id")
    if not project_id:
        raise HTTPException(400, "project_id is required")
    if not doc_id:
        raise HTTPException(400, "doc_id is required")
    if not profile_id:
        raise HTTPException(400, "profile_id is required")
    return {
        "project_id": project_id, "doc_id": doc_id, "profile_id": profile_id,
        "subtitle_occurrence_id": str(subtitle_occurrence_id) if subtitle_occurrence_id else None,
    }


def _compute_plan(owner: str, body: Dict[str, Any]):
    from src.creator.errors import CreatorError
    from src.creator.render import service as render_service
    try:
        return render_service.plan(
            owner=owner, project_id=body["project_id"], doc_id=body["doc_id"],
            profile_id=body["profile_id"], subtitle_occurrence_id=body["subtitle_occurrence_id"],
        )
    except CreatorError as exc:
        raise HTTPException(404 if "no such document" in str(exc) else 400, str(exc))
    except Exception as exc:  # noqa: BLE001 - a bad profile id/graph error is a 400, not a 500
        raise HTTPException(400, str(exc))


def setup_creator_render_routes() -> APIRouter:
    router = APIRouter(prefix="/api/creator/render", tags=["creator-render"])

    @router.post("/plan")
    async def post_plan(request: Request):
        owner = _owner(request)
        _require_flag()
        payload = await _json_object(request)
        body = _parse_render_body(payload)
        render_plan = _compute_plan(owner, body)
        return render_plan.to_dict()

    @router.post("")
    async def post_render(request: Request):
        owner = _owner(request)
        _require_flag()
        payload = await _json_object(request)
        body = _parse_render_body(payload)
        preflight_digest = str(payload.get("preflight_digest") or "")
        session_id = str(payload.get("session_id") or "")

        render_plan = _compute_plan(owner, body)
        av_ids = [i.occurrence_id for i in render_plan.graph.inputs]
        if render_plan.graph.subtitle_occurrence_id:
            av_ids = list(av_ids) + [render_plan.graph.subtitle_occurrence_id]

        from src.creator import profile as profile_mod
        from src.creator.preflight import run_preflight
        from services.projects import get_store as get_project_store

        if get_project_store().get(body["project_id"], owner) is None:
            raise HTTPException(404, "project not found")

        project_profile = profile_mod.get_profile(get_project_store(), owner, body["project_id"]) or {}
        # `estimated_tokens` here is a proxy cost unit for the budget policy
        # (`budget_account`'s own vocabulary is "tokens", not currency) —
        # estimated output KILOBYTES, the one real cost signal a render's
        # own `plan()` already computed. Never used for anything OTHER than
        # feeding the SAME budget_mode/ceiling gate every other Creator
        # operation goes through.
        estimated_kb = max(1, render_plan.estimated_size_bytes // 1024)
        report = run_preflight(
            owner=owner, project_id=body["project_id"], operation="render_timeline",
            engine="ffmpeg", deployment_id="ffmpeg",
            params={"profile_id": body["profile_id"], "doc_id": body["doc_id"],
                    "doc_revision": render_plan.doc_revision, "estimated_tokens": estimated_kb},
            inputs=av_ids, profile=project_profile,
        )

        if report.requires_approval:
            if not preflight_digest:
                raise HTTPException(403, {
                    "reason": "approval_required",
                    "detail": "this render needs approval; call POST /api/creator/preflight "
                              "then /api/creator/preflight/{digest}/approve first",
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
                    "detail": "this approval has already been spent, or the plan changed "
                              "since it was granted",
                })
        elif not report.ok:
            raise HTTPException(400, {"reason": "not_ready", "missing": list(report.missing)})

        from src.creator.render import service as render_service
        started = render_service.start_render(
            owner=owner, project_id=body["project_id"], doc_id=body["doc_id"],
            profile_id=body["profile_id"],
            subtitle_occurrence_id=body["subtitle_occurrence_id"], session_id=session_id,
        )
        return started

    @router.get("/{job_id}")
    async def get_render(request: Request, job_id: str):
        owner = _owner(request)
        _require_flag()
        from src.creator.render import service as render_service
        job = render_service.get_job(job_id, owner=owner)
        if job is None:
            raise HTTPException(404, "no such render job")
        return job

    @router.post("/{job_id}/cancel")
    async def cancel_render(request: Request, job_id: str):
        owner = _owner(request)
        _require_flag()
        from src.creator.render import service as render_service
        result = render_service.cancel_job(job_id, owner=owner)
        if result.get("outcome") == "unknown":
            raise HTTPException(404, "no such render job")
        return result

    return router
