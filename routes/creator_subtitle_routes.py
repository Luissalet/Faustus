"""Creator subtitle routes (WP16), over ``src.creator.subtitles`` +
``src.creator.ops.subtitle_ops``.

Same discipline as every other ``routes/creator_*`` module: gated by the
``creator_enabled`` setting (default False), checked before any store
access; owner always resolved from the authenticated session, never the
body (CONTRATO.md rule 3); a foreign owner's document answers exactly like
a missing one, 404.

Editing/timing/style changes to an existing ``subtitles`` document go
through the generic ``POST /api/creator/documents/{id}/commands`` route
(``routes/creator_routes.py``) with the typed ops this lot registers in
``src/creator/ops/subtitle_ops.py`` — there is no separate "edit cue"
endpoint here, same pattern WP13's ``timeline.*`` ops use. This module adds
only the three things a typed op cannot do on its own: build a fresh
``subtitles`` document from a ``transcript`` one (needs to CREATE a
document, which ``apply_command`` never does), read-only QA, and file
export.
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request, Response

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


def _get_subtitles_doc(store, owner: str, doc_id: str):
    doc = store.get(owner, doc_id)
    if doc is None or doc.kind != "subtitles":
        # A transcript/timeline/etc. document id given here answers exactly
        # like an unknown one — this route only ever speaks about
        # `subtitles` documents (CONTRATO.md rule 3's "no es tuyo"/"no
        # existe" symmetry extended to "not this kind").
        raise HTTPException(404, "subtitles document not found")
    return doc


def setup_creator_subtitle_routes() -> APIRouter:
    router = APIRouter(prefix="/api/creator/subtitles", tags=["creator-subtitles"])

    @router.post("/from-transcript")
    async def from_transcript_route(request: Request, response: Response):
        owner = _owner(request)
        _require_flag()
        payload = await _json_object(request)

        transcript_doc_id = str(payload.get("transcript_doc_id") or "")
        if not transcript_doc_id:
            raise HTTPException(400, "transcript_doc_id is required")
        language = payload.get("language")
        profile_overrides = payload.get("profile_overrides")
        style = payload.get("style")
        if profile_overrides is not None and not isinstance(profile_overrides, dict):
            raise HTTPException(400, "profile_overrides must be an object when given")
        if style is not None and not isinstance(style, dict):
            raise HTTPException(400, "style must be an object when given")

        from src.creator.store import get_store
        from src.creator.errors import InvalidDocument, InvalidOperation
        from src.creator import subtitles as subtitles_mod

        store = get_store()
        transcript_doc = store.get(owner, transcript_doc_id)
        if transcript_doc is None or transcript_doc.kind != "transcript":
            raise HTTPException(404, "transcript document not found")

        try:
            content = subtitles_mod.from_transcript(
                transcript_doc.content,
                language=str(language) if language else None,
                profile_overrides=profile_overrides,
                style=style,
                source_transcript_id=transcript_doc.id,
            )
        except InvalidOperation as exc:
            raise HTTPException(400, str(exc))

        try:
            doc = store.create(owner, transcript_doc.project_id, "subtitles", content,
                               asset_refs=list(transcript_doc.asset_refs))
        except InvalidDocument as exc:
            raise HTTPException(400, str(exc))
        response.headers["ETag"] = str(doc.revision)
        return doc.to_public_dict()

    @router.get("/{doc_id}/qa")
    def qa_route(request: Request, doc_id: str):
        owner = _owner(request)
        _require_flag()
        from src.creator.store import get_store
        from src.creator import subtitles as subtitles_mod

        doc = _get_subtitles_doc(get_store(), owner, doc_id)
        return {"doc_id": doc.id, "revision": doc.revision, "report": subtitles_mod.qa_report(doc.content)}

    @router.get("/{doc_id}/export")
    def export_route(request: Request, doc_id: str, format: str = "srt"):
        owner = _owner(request)
        _require_flag()
        from src.creator.store import get_store
        from src.creator import subtitles as subtitles_mod
        from src.creator.errors import InvalidOperation

        doc = _get_subtitles_doc(get_store(), owner, doc_id)
        try:
            out = subtitles_mod.export(doc.content, format)
        except InvalidOperation as exc:
            raise HTTPException(400, str(exc))
        return Response(
            out["text"], media_type=out["media_type"],
            headers={
                "Content-Disposition": f'attachment; filename="{out["filename"]}"',
                "Cache-Control": "no-store",
            },
        )

    return router
