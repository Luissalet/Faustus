"""Creator voice/TTS routes (WP18), over ``src.creator.voices`` /
``src.creator.adapters.tts`` / ``src.creator.consent``.

Same discipline as every other ``routes/creator_*`` module: gated by the
``creator_enabled`` setting (default False), checked before any store
access; owner always resolved from the authenticated session, never the
body (CONTRATO.md rule 3); a foreign project/document answers exactly like
a missing one, 404. Long-running work runs in ``asyncio.to_thread``
(CONTRATO.md rule 6) — synthesis itself is fast (the tts adapter is
synchronous, queue-of-one) but sqlite/consent reads still leave the event
loop.

``POST /api/creator/voices/synthesize`` is the one route the ficha calls
out as requiring an approved preflight: it runs
``src.creator.preflight.run_preflight`` for the ``tts``/``voice_clone``/
``dub`` operation first (WP09, already built) and, when that reports
``requires_approval``, checks ``src.approval_store.check()`` for a granted
card covering the EXACT plan preflight just computed — a caller gets 402
with the digest to approve (via ``POST /api/creator/preflight/{digest}/
approve``, WP09's own route) rather than the render silently going ahead
un-approved.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request

from src.auth_helpers import require_user


def _owner(request: Request) -> str:
    from core import middleware as mw
    from src.owner_identity import effective_storage_owner
    user = require_user(request)
    return effective_storage_owner(user, auth_is_disabled=mw.auth_disabled()) or ""


def _current_user(request: Request) -> str:
    return str(getattr(request.state, "current_user", "") or "").strip()


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


def _project_or_404(owner: str, project_id: str):
    from services.projects import get_store as get_project_store
    project = get_project_store().get(project_id, owner)
    if project is None:
        raise HTTPException(404, "project not found")
    return project


def _voice_error_to_400(exc) -> None:
    raise HTTPException(400, str(exc))


def setup_creator_voice_routes() -> APIRouter:
    router = APIRouter(prefix="/api/creator/voices", tags=["creator-voices"])

    # ── catalog ─────────────────────────────────────────────────────────

    @router.get("")
    async def list_voices_route(request: Request, language: str = ""):
        _owner(request)
        _require_flag()
        from src.creator import voices

        catalog = await asyncio.to_thread(voices.list_voices, language=language or None)
        return catalog

    # ── audition ────────────────────────────────────────────────────────

    @router.post("/audition")
    async def audition_route(request: Request):
        owner = _owner(request)
        _require_flag()
        payload = await _json_object(request)

        project_id = str(payload.get("project_id") or "")
        voice_id = str(payload.get("voice_id") or "")
        text = payload.get("text")
        language = payload.get("language")
        speed = payload.get("speed", 1.0)
        if not project_id:
            raise HTTPException(400, "project_id is required")
        if not voice_id:
            raise HTTPException(400, "voice_id is required")
        if not isinstance(text, str) or not text.strip():
            raise HTTPException(400, "text is required")
        if not isinstance(speed, (int, float)) or isinstance(speed, bool):
            raise HTTPException(400, "speed must be a number")

        _project_or_404(owner, project_id)

        from src.creator import voices

        try:
            return await asyncio.to_thread(
                voices.audition, owner, project_id, voice_id, text,
                language=str(language) if language else None, speed=float(speed),
            )
        except voices.VoiceError as exc:
            _voice_error_to_400(exc)

    # ── casting ─────────────────────────────────────────────────────────

    @router.get("/casting")
    async def get_casting_route(request: Request, project_id: str, doc_id: str):
        owner = _owner(request)
        _require_flag()
        if not project_id or not doc_id:
            raise HTTPException(400, "project_id and doc_id are required")
        _project_or_404(owner, project_id)
        from src.creator import voices

        casting = await asyncio.to_thread(voices.get_casting, owner, project_id, doc_id)
        return {"doc_id": doc_id, "casting": casting}

    @router.post("/casting")
    async def set_casting_route(request: Request):
        owner = _owner(request)
        _require_flag()
        payload = await _json_object(request)

        project_id = str(payload.get("project_id") or "")
        doc_id = str(payload.get("doc_id") or "")
        assignments = payload.get("assignments")
        if not project_id or not doc_id:
            raise HTTPException(400, "project_id and doc_id are required")
        if not isinstance(assignments, dict) or not assignments:
            raise HTTPException(400, "assignments must be a non-empty object of {speaker_id: voice_id}")

        _project_or_404(owner, project_id)

        from src.creator import voices

        try:
            return await asyncio.to_thread(voices.set_casting, owner, project_id, doc_id, assignments)
        except voices.VoiceError as exc:
            _voice_error_to_400(exc)

    # ── synthesize (exige preflight aprobado) ──────────────────────────

    @router.post("/synthesize")
    async def synthesize_route(request: Request):
        owner = _owner(request)
        _require_flag()
        payload = await _json_object(request)

        project_id = str(payload.get("project_id") or "")
        doc_id = str(payload.get("doc_id") or "")
        default_voice_id = payload.get("default_voice_id")
        cue_ids = payload.get("cue_ids")
        replace_original = bool(payload.get("replace_original", False))
        if not project_id or not doc_id:
            raise HTTPException(400, "project_id and doc_id are required")
        if cue_ids is not None and not isinstance(cue_ids, list):
            raise HTTPException(400, "cue_ids must be a list")

        _project_or_404(owner, project_id)

        from src.creator import voices  # noqa: F401 - registers the (tts, tts) param schema on import
        from src.creator import preflight as preflight_mod
        from src.creator import profile as profile_mod
        from services.projects import get_store as get_project_store

        def _preflight():
            prof = profile_mod.get_profile(get_project_store(), owner, project_id) or {}
            return preflight_mod.run_preflight(
                owner=owner, project_id=project_id, operation="tts", engine="tts",
                deployment_id="tts", params={}, inputs=(), profile=prof,
            )

        report = await asyncio.to_thread(_preflight)

        if report.requires_approval:
            from src import approval_store
            from src.contracts import ApprovalPlan

            plan = ApprovalPlan.parse(report.approval_plan)
            checked = await asyncio.to_thread(approval_store.check, plan, owner=owner)
            if not checked.get("ok"):
                raise HTTPException(402, {
                    "reason": "approval_required",
                    "detail": "this synthesis needs an approved preflight before it can run",
                    "approval_digest": report.approval_digest,
                    "preflight": report.to_dict(),
                })
            await asyncio.to_thread(approval_store.consume, checked["approval_id"], plan, owner=owner)
        else:
            # `report.missing` can include a `"capability"` entry purely
            # because WP07's `model_identity` registry has no evidenced
            # deployment named "tts" (this local adapter is not a chat-model
            # deployment WP06 ever registered) — that gap is not a reason to
            # block a synthesis this adapter's OWN `describe()`/`plan()`
            # (the real availability/consent gate, checked again inside
            # `voices.synthesize_transcript`) says it can actually do.
            # Every OTHER missing kind (consent, param, input) and a failed
            # VRAM/budget check still block here, same as `report.ok` would.
            blocking = [m for m in report.missing if m.get("kind") != "capability"]
            if blocking or not report.admission.fits:
                raise HTTPException(400, {"reason": "preflight_failed", "preflight": report.to_dict()})

        from src.creator import voices

        try:
            return await asyncio.to_thread(
                voices.synthesize_transcript, owner, project_id, doc_id,
                default_voice_id=str(default_voice_id) if default_voice_id else None,
                cue_ids=[str(c) for c in cue_ids] if cue_ids else None,
                replace_original=replace_original,
            )
        except voices.VoiceError as exc:
            _voice_error_to_400(exc)

    return router


def setup_creator_consent_routes() -> APIRouter:
    """``POST``/``DELETE /api/creator/consent`` — the WP18 ficha's own
    consent endpoints, separate from the ``/voices`` prefix since consent
    (owner+subject+scope) is not itself a voice."""
    router = APIRouter(prefix="/api/creator/consent", tags=["creator-consent"])

    @router.get("")
    async def list_consent_route(request: Request, subject: str = ""):
        owner = _owner(request)
        _require_flag()
        from src.creator import consent as consent_mod

        if subject:
            records = await asyncio.to_thread(consent_mod.for_subject, owner, subject)
        else:
            records = await asyncio.to_thread(consent_mod.list_for_owner, owner)
        return {"consent": records}

    @router.post("")
    async def register_consent_route(request: Request):
        owner = _owner(request)
        _require_flag()
        payload = await _json_object(request)

        subject = str(payload.get("subject") or "")
        scope = str(payload.get("scope") or "clone")
        evidence_ref = str(payload.get("evidence_ref") or "")
        note = str(payload.get("note") or "")
        expires_at = payload.get("expires_at")
        if expires_at is not None and not isinstance(expires_at, (int, float)):
            raise HTTPException(400, "expires_at must be a unix timestamp number")

        granted_by = _current_user(request) or owner
        from src.creator import consent as consent_mod

        result = await asyncio.to_thread(
            consent_mod.register, owner, subject, granted_by=granted_by, scope=scope,
            evidence_ref=evidence_ref, note=note, expires_at=expires_at,
        )
        if not result.get("ok"):
            raise HTTPException(400, result)
        return result

    @router.delete("/{consent_id}")
    async def revoke_consent_route(request: Request, consent_id: str):
        owner = _owner(request)
        _require_flag()
        from src.creator import consent as consent_mod

        result = await asyncio.to_thread(consent_mod.revoke, owner, consent_id)
        if not result.get("ok"):
            raise HTTPException(404, result)
        return result

    return router
