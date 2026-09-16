"""Creator Music Studio routes (WP24), over ``src.creator.music`` /
``src.creator.adapters.music``.

Same discipline as every other ``routes/creator_*`` module: gated by the
``creator_enabled`` setting (default False, checked before any store
access, CONTRATO.md rule 5); owner always resolved from the authenticated
session, never the body (rule 3); a foreign owner's job/document answers
exactly like a missing one, 404.

* ``POST /api/creator/music/generate`` — starts a generation for an
  existing ``song`` document. **Requires an approved preflight** when
  ``src.creator.preflight.run_preflight`` says this plan needs one
  (``requires_approval``): the caller must have already run
  ``POST /api/creator/preflight`` for ``operation="generate_music"`` and
  approved the resulting digest via
  ``POST /api/creator/preflight/{digest}/approve`` (a human-gated route —
  see that module), then pass the SAME digest back here as
  ``preflight_digest``. This route recomputes the preflight itself (never
  trusts a client-supplied "it was approved") and checks
  ``src.approval_store.check()`` for a granted card covering the recomputed
  plan — the exact same digest-binding discipline
  ``creator_preflight_routes.py``'s own `/approve` uses. A plan that does
  not currently require approval (small/local/no-budget-gate) skips this
  check entirely, same as every other Creator operation.
* ``GET /api/creator/music/{job_id}`` — current state; once the underlying
  job completes, this call is also what finishes it (registers the
  occurrence, records the take on the document) the first time it is
  polled past completion — same pattern as
  ``GET /api/creator/asr/{job_id}``.
* ``POST /api/creator/music/{doc_id}/variants`` — starts ``n`` independent
  generations of the same document with distinct seeds; also gated by the
  same preflight-approval check, run once for the batch's shared params
  (one approval covers the whole batch, not one per variant — variants
  differ only by seed, which the budget/approval plan does not key on).
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request


def _owner(request: Request) -> str:
    from core import middleware as mw
    from src.owner_identity import effective_storage_owner
    from src.auth_helpers import require_user
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


def _require_approved_preflight(*, owner: str, project_id: str, engine: str,
                                deployment_id: str, params: Dict[str, Any],
                                preflight_digest: str) -> None:
    """Recomputes the SAME preflight `generate()`/`variants()` would need
    and, if it requires approval, insists a granted card already covers it
    — 403 otherwise, naming the digest to approve. Mirrors
    `creator_preflight_routes.py`'s own digest-binding discipline; this
    route never grants an approval itself (that stays a human-gated call
    on `/api/creator/preflight/{digest}/approve`)."""
    from src.creator import profile as profile_mod
    from src.creator.preflight import run_preflight
    from services.projects import get_store as get_project_store

    profile = profile_mod.get_profile(get_project_store(), owner, project_id) or {}
    report = run_preflight(
        owner=owner, project_id=project_id, operation="generate_music",
        engine=engine, deployment_id=deployment_id, params=params, inputs=(),
        profile=profile,
    )
    if not report.requires_approval:
        return
    if not preflight_digest:
        raise HTTPException(
            403,
            {"reason": "preflight_required",
             "detail": "this generation needs an approved preflight; run POST /api/creator/preflight "
                       "and approve the resulting digest, then pass it back as 'preflight_digest'",
             "digest": report.approval_digest},
        )
    if report.approval_digest != preflight_digest:
        raise HTTPException(
            409,
            {"reason": "digest_mismatch",
             "detail": "the plan behind this digest has changed since preflight; re-run preflight",
             "current_digest": report.approval_digest},
        )
    from src import approval_store
    check = approval_store.check(report.approval_plan, owner=owner)
    if not check.get("ok"):
        raise HTTPException(
            403,
            {"reason": check.get("reason", "no_approval"),
             "detail": check.get("detail", "no granted approval covers this plan yet"),
             "digest": preflight_digest},
        )


def _parse_generate_body(payload: Dict[str, Any]) -> Dict[str, Any]:
    doc_id = str(payload.get("doc_id") or "")
    if not doc_id:
        raise HTTPException(400, "doc_id is required")
    project_id = payload.get("project_id")
    engine = payload.get("engine")
    deployment_id = str(payload.get("deployment_id") or "")
    duration_s = payload.get("duration_s")
    if duration_s is not None and (not isinstance(duration_s, (int, float)) or isinstance(duration_s, bool)):
        raise HTTPException(400, "duration_s must be a number")
    steps = payload.get("steps")
    if steps is not None and (not isinstance(steps, int) or isinstance(steps, bool)):
        raise HTTPException(400, "steps must be an integer")
    seed = payload.get("seed")
    if seed is not None and (not isinstance(seed, int) or isinstance(seed, bool)):
        raise HTTPException(400, "seed must be an integer")
    device = payload.get("device")
    reference_occurrence_id = payload.get("reference_occurrence_id")
    preflight_digest = str(payload.get("preflight_digest") or "")
    return {
        "doc_id": doc_id, "project_id": str(project_id) if project_id else None,
        "engine": str(engine) if engine else None, "deployment_id": deployment_id,
        "duration_s": float(duration_s) if duration_s is not None else None,
        "steps": steps, "seed": seed, "device": str(device) if device else None,
        "reference_occurrence_id": str(reference_occurrence_id) if reference_occurrence_id else None,
        "preflight_digest": preflight_digest,
    }


def setup_creator_music_routes() -> APIRouter:
    router = APIRouter(prefix="/api/creator/music", tags=["creator-music"])

    @router.post("/generate")
    async def generate_music_route(request: Request):
        owner = _owner(request)
        _require_flag()
        payload = await _json_object(request)
        body = _parse_generate_body(payload)

        from src.creator import music, store as store_mod

        doc = await asyncio.to_thread(store_mod.get_store().get, owner, body["doc_id"])
        if doc is None:
            raise HTTPException(404, "document not found")
        if doc.kind != "song":
            raise HTTPException(400, f"document is kind={doc.kind!r}; music generation needs a 'song' document")
        project_id = body["project_id"] or doc.project_id

        preflight_params: Dict[str, Any] = {
            "duration_s": body["duration_s"] or music.DEFAULT_DURATION_S,
        }
        if body["engine"]:
            preflight_params["engine"] = body["engine"]
        await asyncio.to_thread(
            _require_approved_preflight, owner=owner, project_id=project_id,
            engine=body["engine"] or "acestep", deployment_id=body["deployment_id"],
            params=preflight_params, preflight_digest=body["preflight_digest"],
        )

        try:
            job = await asyncio.to_thread(
                music.generate, owner, body["doc_id"], project_id=project_id,
                duration_s=body["duration_s"], engine=body["engine"], steps=body["steps"],
                seed=body["seed"], device=body["device"],
                reference_occurrence_id=body["reference_occurrence_id"],
            )
        except music.MusicJobNotFound:
            raise HTTPException(404, "document not found")
        except music.MusicError as exc:
            raise HTTPException(400, str(exc))
        return job

    @router.get("/{job_id}")
    async def get_music_job_route(request: Request, job_id: str):
        owner = _owner(request)
        _require_flag()
        from src.creator import music

        job = await asyncio.to_thread(music.get_job, owner, job_id)
        if job is None:
            raise HTTPException(404, "music job not found")
        return job

    @router.post("/{job_id}/cancel")
    async def cancel_music_job_route(request: Request, job_id: str):
        owner = _owner(request)
        _require_flag()
        from src.creator import music

        try:
            return await asyncio.to_thread(music.cancel, owner, job_id)
        except music.MusicJobNotFound:
            raise HTTPException(404, "music job not found")

    @router.post("/{doc_id}/variants")
    async def generate_variants_route(request: Request, doc_id: str):
        owner = _owner(request)
        _require_flag()
        payload = await _json_object(request)
        n = payload.get("n")
        if not isinstance(n, int) or isinstance(n, bool) or n < 1:
            raise HTTPException(400, "n must be a positive integer")
        body = _parse_generate_body({**payload, "doc_id": doc_id})

        from src.creator import music, store as store_mod

        doc = await asyncio.to_thread(store_mod.get_store().get, owner, doc_id)
        if doc is None:
            raise HTTPException(404, "document not found")
        if doc.kind != "song":
            raise HTTPException(400, f"document is kind={doc.kind!r}; music generation needs a 'song' document")
        project_id = body["project_id"] or doc.project_id

        preflight_params: Dict[str, Any] = {
            "duration_s": body["duration_s"] or music.DEFAULT_DURATION_S,
        }
        if body["engine"]:
            preflight_params["engine"] = body["engine"]
        await asyncio.to_thread(
            _require_approved_preflight, owner=owner, project_id=project_id,
            engine=body["engine"] or "acestep", deployment_id=body["deployment_id"],
            params=preflight_params, preflight_digest=body["preflight_digest"],
        )

        jobs = await asyncio.to_thread(
            music.variants, owner, doc_id, n, project_id=project_id,
            duration_s=body["duration_s"], engine=body["engine"], steps=body["steps"],
            device=body["device"], base_seed=body["seed"],
        )
        return {"jobs": jobs}

    return router
