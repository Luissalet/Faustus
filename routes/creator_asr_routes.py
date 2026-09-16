"""Creator ASR routes (WP15), over ``src.creator.asr`` /
``src.creator.adapters.whisper``.

Same discipline as every other ``routes/creator_*`` module: gated by the
``creator_enabled`` setting (default False), checked before any store
access; owner always resolved from the authenticated session, never the
body (CONTRATO.md rule 3); a foreign owner's job answers exactly like a
missing one, 404.

Two routes, exactly the ficha's shape:

* ``POST /api/creator/asr`` — ``{occurrence_id, language?, align_words?,
  model?, device?, diarize?, project_id?}``. Starts a transcription; the
  CPU-bound staging/normalize/plan/submit work runs in a worker thread
  (CONTRATO.md rule 6: "trabajo síncrono largo en asyncio.to_thread"), the
  transcription ITSELF runs asynchronously in the adapter's own worker
  pool, so this route returns as soon as the job is queued, not once it
  finishes.
* ``GET /api/creator/asr/{job_id}`` — current state; once the underlying
  ASR job completes, this call is also what turns it into a `transcript`
  document (``asr.get_job`` does that synchronously the first time it is
  polled past completion), so a caller does not need a third endpoint to
  "finalize" a finished job.
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


def setup_creator_asr_routes() -> APIRouter:
    router = APIRouter(prefix="/api/creator/asr", tags=["creator-asr"])

    @router.post("")
    async def submit_asr_route(request: Request):
        owner = _owner(request)
        _require_flag()
        payload = await _json_object(request)

        occurrence_id = str(payload.get("occurrence_id") or "")
        if not occurrence_id:
            raise HTTPException(400, "occurrence_id is required")
        project_id = payload.get("project_id")
        language = payload.get("language")
        if language is not None:
            language = str(language)
        align_words = payload.get("align_words", True)
        if not isinstance(align_words, bool):
            raise HTTPException(400, "align_words must be a boolean")
        model = payload.get("model")
        device = payload.get("device")
        diarize = payload.get("diarize", False)
        if not isinstance(diarize, bool):
            raise HTTPException(400, "diarize must be a boolean")

        from src.creator import asr

        try:
            job = await asyncio.to_thread(
                asr.submit, owner, occurrence_id,
                project_id=str(project_id) if project_id else None,
                language=language, align_words=align_words,
                model=str(model) if model else None,
                device=str(device) if device else None,
                diarize=diarize,
            )
        except asr.AsrJobNotFound:
            raise HTTPException(404, "occurrence not found")
        except asr.AsrError as exc:
            raise HTTPException(400, str(exc))
        return job

    @router.get("/{job_id}")
    async def get_asr_job_route(request: Request, job_id: str):
        owner = _owner(request)
        _require_flag()
        from src.creator import asr

        job = await asyncio.to_thread(asr.get_job, owner, job_id)
        if job is None:
            raise HTTPException(404, "ASR job not found")
        return job

    @router.post("/{job_id}/cancel")
    async def cancel_asr_job_route(request: Request, job_id: str):
        owner = _owner(request)
        _require_flag()
        from src.creator import asr

        try:
            return await asyncio.to_thread(asr.cancel, owner, job_id)
        except asr.AsrJobNotFound:
            raise HTTPException(404, "ASR job not found")

    return router
