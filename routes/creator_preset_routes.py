"""Presets, evaluation and discovery — WP41, over `src/creator/presets.py`.

  GET  /api/creator/presets?engine=&task=              This owner's presets,
                                                         optionally narrowed.
  POST /api/creator/presets                             Create a preset
                                                         (validated against
                                                         `params.py`).
  POST /api/creator/presets/{id}/rate                   Append an artistic
                                                         1-5 rating + comment.
  POST /api/creator/presets/{id}/evaluate                Run `presets.
                                                         evaluate()` against a
                                                         fixture run and store
                                                         it as technical
                                                         evidence. Never
                                                         promotes anything.
  POST /api/creator/presets/{id}/promote                 Promote to
                                                         `recommended` via
                                                         `harness_evolution`
                                                         (held-out gate;
                                                         rejection is kept as
                                                         evidence, not a
                                                         silent no-op).
  POST /api/creator/presets/{id}/rollback                Undo a promotion.
  GET  /api/creator/presets/discover?project_id=         Propose candidates
                                                         from this owner's
                                                         own completed,
                                                         well-rated runs.

Gated on `creator_enabled` (CONTRATO rule 5, default False), checked BEFORE
any store access — with the flag off every route below answers 404 and
nothing is read or written. Owner is always resolved from the authenticated
session (CONTRATO rule 3), never from the request body; a preset id that
belongs to someone else answers exactly like one that does not exist: 404.
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Query, Request

from src.auth_helpers import require_user
from src.creator import presets as presets_mod


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


def _preset_error(exc: Exception) -> None:
    if isinstance(exc, presets_mod.PresetNotFound):
        raise HTTPException(404, "preset not found")
    if isinstance(exc, presets_mod.PresetRevisionConflict):
        raise HTTPException(409, {"reason": "revision_conflict",
                                  "current_revision": exc.current_revision})
    if isinstance(exc, presets_mod.PromotionRejected):
        raise HTTPException(409, {"reason": "held_out_evaluation_failed",
                                  "patch_id": exc.patch_id, "evaluation": exc.evaluation})
    if isinstance(exc, presets_mod.InvalidPreset):
        raise HTTPException(400, str(exc))
    raise exc


def setup_creator_preset_routes() -> APIRouter:
    router = APIRouter(prefix="/api/creator/presets", tags=["creator-presets"])

    @router.get("")
    def list_presets(request: Request, engine: str = "", task: str = ""):
        owner = _owner(request)
        _require_flag()
        rows = presets_mod.get_store().list_for_owner(owner, engine=engine, task=task)
        return {"presets": [p.to_dict() for p in rows]}

    @router.get("/discover")
    def discover(request: Request,
                project_id: str = Query(..., description="Project to discover presets from")):
        owner = _owner(request)
        _require_flag()
        if not project_id:
            raise HTTPException(400, "project_id is required")
        # This route never fabricates ratings: without a real rating source
        # wired in yet, discovery here always proposes nothing (an empty,
        # honest result) rather than guessing "good" from mere completion.
        # A future caller with a real rating store passes it to
        # `discover_from_runs(..., run_ratings=...)` directly (agent tool /
        # background job), not through this read-only GET.
        proposed = presets_mod.discover_from_runs(owner, project_id, run_ratings={})
        return {"presets": [p.to_dict() for p in proposed]}

    @router.post("")
    async def create_preset(request: Request):
        owner = _owner(request)
        _require_flag()
        payload = await _json_object(request)
        engine = str(payload.get("engine") or "").strip()
        task = str(payload.get("task") or "").strip()
        params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
        deployment_ids = payload.get("deployment_ids") or []
        origin = str(payload.get("origin") or presets_mod.ORIGIN_USER)
        if not engine or not task:
            raise HTTPException(400, "engine and task are required")
        if not isinstance(deployment_ids, list):
            raise HTTPException(400, "deployment_ids must be a list")
        try:
            preset = presets_mod.get_store().create(
                owner, engine, task, params, deployment_ids=deployment_ids, origin=origin)
        except presets_mod.InvalidPreset as exc:
            raise HTTPException(400, str(exc))
        return preset.to_dict()

    def _command_fields(payload: Dict[str, Any]) -> tuple:
        command_id = str(payload.get("command_id") or "")
        if not command_id:
            raise HTTPException(400, "command_id is required")
        if "expected_revision" not in payload:
            raise HTTPException(400, "expected_revision is required")
        try:
            expected_revision = int(payload.get("expected_revision"))
        except (TypeError, ValueError):
            raise HTTPException(400, "expected_revision must be an integer")
        return command_id, expected_revision

    @router.post("/{preset_id}/rate")
    async def rate_preset(request: Request, preset_id: str):
        owner = _owner(request)
        _require_flag()
        payload = await _json_object(request)
        command_id, expected_revision = _command_fields(payload)
        if "rating" not in payload:
            raise HTTPException(400, "rating is required")
        try:
            rating = int(payload.get("rating"))
        except (TypeError, ValueError):
            raise HTTPException(400, "rating must be an integer")
        comment = str(payload.get("comment") or "")
        try:
            preset = presets_mod.get_store().rate(
                owner, preset_id, rating=rating, comment=comment, rated_by=owner,
                command_id=command_id, expected_revision=expected_revision)
        except Exception as exc:  # narrowed by _preset_error
            _preset_error(exc)
            raise
        return preset.to_dict()

    @router.post("/{preset_id}/evaluate")
    async def evaluate_preset(request: Request, preset_id: str):
        owner = _owner(request)
        _require_flag()
        payload = await _json_object(request)
        command_id, expected_revision = _command_fields(payload)
        fixture_run = payload.get("fixture_run")
        if not isinstance(fixture_run, dict):
            raise HTTPException(400, "fixture_run must be an object")
        store = presets_mod.get_store()
        preset = store.get(owner, preset_id)
        if preset is None:
            raise HTTPException(404, "preset not found")
        record = presets_mod.evaluate(preset, fixture_run)
        try:
            preset = store.record_evaluation(
                owner, preset_id, record, command_id=command_id,
                expected_revision=expected_revision)
        except Exception as exc:  # narrowed by _preset_error
            _preset_error(exc)
            raise
        return {"preset": preset.to_dict(), "evaluation": record.to_dict()}

    @router.post("/{preset_id}/promote")
    async def promote_preset_route(request: Request, preset_id: str):
        owner = _owner(request)
        _require_flag()
        payload = await _json_object(request)
        held_out_task_ids = payload.get("held_out_task_ids")
        source_task_ids = payload.get("source_task_ids")
        try:
            preset = presets_mod.promote_preset(
                owner, preset_id, actor=owner or "system",
                source_task_ids=source_task_ids if isinstance(source_task_ids, list) else None,
                held_out_task_ids=held_out_task_ids if isinstance(held_out_task_ids, list) else None,
            )
        except Exception as exc:  # narrowed by _preset_error
            _preset_error(exc)
            raise
        return preset.to_dict()

    @router.post("/{preset_id}/rollback")
    async def rollback_preset_route(request: Request, preset_id: str):
        owner = _owner(request)
        _require_flag()
        payload = await _json_object(request)
        reason = str(payload.get("reason") or "")
        try:
            preset = presets_mod.rollback_preset(
                owner, preset_id, actor=owner or "system", reason=reason)
        except Exception as exc:  # narrowed by _preset_error
            _preset_error(exc)
            raise
        return preset.to_dict()

    return router


__all__ = ["setup_creator_preset_routes"]
