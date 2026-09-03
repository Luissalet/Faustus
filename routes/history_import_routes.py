"""Imported history API — /api/history/* (src/history_import.py).

NOTE ON THE MODULE NAME: this is ``history_import_routes``, not
``history_routes`` — that name is already taken by the compatibility shim for
``routes/history/history_routes.py``, which is the CHAT history (compaction,
forking, the session transcript). Two unrelated features called "history" is
confusing enough without them also colliding in ``sys.modules``; this one is
the *imported* history, somebody else's export brought here.

The human end of "bring your whole past here": point it at a ChatGPT or
Claude data export, an LM Studio chat folder, or one of Faustus's own JSON
exports; see exactly what it *would* do; then let it do it. After that the
archive is a normal, searchable part of the app.

Admin-only, like the rest of the brain: an import writes somebody's entire
conversational history into this machine's store, and the reads hand it back.

Two things this router refuses to paper over:

* **The dry run is the default answer to "what will this do?"** ``POST
  /import`` with ``dry_run`` reports the counts, the detected source and every
  skipped conversation WITH its reason, and writes nothing — not a row, not
  even the database file. The page shows that summary and asks before the
  real run.
* **A skipped conversation is part of the response, not a log line.** If four
  of a user's nine hundred conversations could not be read, the response says
  which four and why.

The reads a coordinating model would want — the list, one conversation, the
search and the stats — also answer in robot mode (``?robot=1`` /
``?format=toon``, src/robot_envelope.py) with the LEAN projections at the
bottom of this file: one flat row per conversation / per hit. A call without
those query parameters answers exactly as it otherwise would.
"""

from __future__ import annotations

import contextlib
import logging
import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool

from core.middleware import require_admin
from src import robot_envelope as robot

logger = logging.getLogger(__name__)

# Uploads are streamed to disk in blocks — a 400 MB conversations.json is
# never held in memory, the same posture as the expert corpus upload.
READ_CHUNK_BYTES = 1 << 20
MAX_UPLOAD_NAME = 180


def _safe_upload_name(name: Any) -> str:
    """A basename that cannot escape the upload folder, always ``.json``."""
    base = os.path.basename(str(name or "").strip().replace("\\", "/"))
    base = "".join(ch for ch in base if ch.isalnum() or ch in "._- ").strip(" .")
    if not base:
        base = "import.json"
    if not base.lower().endswith(".json"):
        base += ".json"
    return base[:MAX_UPLOAD_NAME]


def setup_history_import_routes() -> APIRouter:
# NOT "/api/history": the chat history already owns
# `GET /api/history/{session_id}` (routes/history/history_routes.py), and that
# path parameter swallows every sibling — live, `/api/history/conversations`
# and `/api/history/stats` came back "Session conversations not found",
# because FastAPI matched them as a session id. Only `POST /import` survived,
# since the older router has no POST. The imported archive is its own resource
# and gets its own prefix.
    router = APIRouter(prefix="/api/history-import", tags=["history-import"])

    # ── import ────────────────────────────────────────────────────────────

    @router.post("/import")
    async def import_history(request: Request,
                             _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        """Import a path on this machine, or an uploaded export file.

        Both bodies are accepted on the one route because they are the same
        action; the content type decides which. A JSON body carries
        ``{"path", "source", "dry_run"}``; a multipart body carries a
        ``file`` field plus the same two options as form fields.

        An uploaded file is streamed to ``DATA_DIR/history_uploads/`` so the
        stored ``path`` points at something that still exists afterwards. A
        DRY RUN deletes its upload again — a preview leaves nothing behind.
        """
        from src import history_import as history

        content_type = (request.headers.get("content-type") or "").lower()
        upload_path: Optional[str] = None
        dry_run = False
        try:
            if content_type.startswith("multipart/"):
                form = await request.form()
                upload = form.get("file")
                if upload is None or not hasattr(upload, "read"):
                    raise HTTPException(status_code=400,
                                        detail="no file field in the upload")
                source = str(form.get("source") or "").strip() or None
                dry_run = str(form.get("dry_run") or "").strip().lower() in (
                    "1", "true", "yes", "on")
                target_dir = history.uploads_dir()
                os.makedirs(target_dir, exist_ok=True)
                upload_path = os.path.join(
                    target_dir, _safe_upload_name(getattr(upload, "filename", "")))
                with open(upload_path, "wb") as handle:
                    while True:
                        block = await upload.read(READ_CHUNK_BYTES)
                        if not block:
                            break
                        await run_in_threadpool(handle.write, block)
                path = upload_path
            else:
                try:
                    body = await request.json()
                except Exception:  # noqa: BLE001 - an empty or bad body is a 400
                    body = {}
                if not isinstance(body, dict):
                    body = {}
                path = str(body.get("path") or "").strip()
                source = str(body.get("source") or "").strip() or None
                dry_run = bool(body.get("dry_run"))
                if not path:
                    raise HTTPException(
                        status_code=400,
                        detail="give a path to import, or upload a file")

            try:
                result = await run_in_threadpool(
                    history.import_path, path, source=source, dry_run=dry_run)
            except history.HistoryImportError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            return {"status": "success", **result,
                    "uploaded": bool(upload_path) and not dry_run}
        finally:
            # A preview leaves nothing behind, including its own upload.
            if upload_path and dry_run:
                with contextlib.suppress(OSError):
                    os.remove(upload_path)

    # ── the conversations ─────────────────────────────────────────────────

    @router.get("/conversations")
    async def list_history(request: Request,
                           source: Optional[str] = Query(default=None),
                           q: str = Query(default=""),
                           limit: int = Query(default=100),
                           offset: int = Query(default=0),
                           _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        """Imported conversations, newest first. ``q`` filters on the TITLE;
        the bodies are what ``/search`` is for."""
        from src import history_import as history

        def payload() -> Dict[str, Any]:
            return {
                "status": "success",
                "conversations": history.list_conversations(
                    source=source, q=q, limit=limit, offset=offset),
                "stats": history.stats(),
                "sources": list(history.SOURCES),
                "enabled": history.enabled(),
            }
        if robot.wants(request):
            return await robot.reply(request, lambda: lean_conversations(payload()))
        return payload()

    @router.get("/conversations/{conversation_id}")
    async def read_history(request: Request, conversation_id: str,
                           _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        """One conversation with every message, in order."""
        from src import history_import as history

        async def payload() -> Dict[str, Any]:
            found = await run_in_threadpool(history.get_conversation, conversation_id)
            if not found:
                raise HTTPException(status_code=404, detail="no such conversation")
            return {"status": "success", "conversation": found}
        if robot.wants(request):
            # The 404 goes through the envelope too, so a coordinator reads
            # one shape whether the call worked or not.
            return await robot.reply(request, payload)
        return await payload()

    @router.delete("/conversations/{conversation_id}")
    async def remove_history(conversation_id: str,
                             _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        from src import history_import as history
        if not await run_in_threadpool(history.delete_conversation, conversation_id):
            raise HTTPException(status_code=404, detail="no such conversation")
        return {"status": "success", "deleted": True, "id": conversation_id}

    # ── search ────────────────────────────────────────────────────────────

    @router.get("/search")
    async def search_history(request: Request, q: str = Query(default=""),
                             k: int = Query(default=10),
                             source: Optional[str] = Query(default=None),
                             _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        """Two-tier search over every imported message.

        ``tier`` and ``degraded`` are part of the answer, always: a result
        list that does not say which lanes produced it is a result list the
        user cannot judge.
        """
        from src import history_import as history

        async def payload() -> Dict[str, Any]:
            found = await run_in_threadpool(history.search, q, k, source=source)
            return {"status": "success", **found}

        async def lean() -> Dict[str, Any]:
            return lean_search(await payload())
        if robot.wants(request):
            return await robot.reply(request, lean)
        return await payload()

    @router.get("/stats")
    async def history_stats(request: Request,
                            _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        """How much past is in here, per source, and over what span."""
        from src import history_import as history

        def payload() -> Dict[str, Any]:
            return {"status": "success", **history.stats(),
                    "known_sources": list(history.SOURCES)}
        if robot.wants(request):
            return await robot.reply(request, payload)
        return payload()

    return router


# ---------------------------------------------------------------------------
# Robot-mode projections (the shared envelope is src/robot_envelope.py; the
# shared projections live in src/robot_projection.py, which is not this
# feature's file — so these two live here, in the module that owns them.)
# ---------------------------------------------------------------------------

_TEXT_CELL = 200


def _cell(value: Any, limit: int = _TEXT_CELL) -> str:
    """One line, bounded — a table row is a line, so a cell cannot hold one."""
    text = " ".join(str(value if value is not None else "").split())
    return text[:limit]


def _int_cell(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def lean_conversations(payload: Dict[str, Any]) -> Dict[str, Any]:
    """The archive as rows: where each conversation came from and when.

    ``started_at`` stays nullable on purpose. A conversation whose date the
    export did not record reads as ``null`` here too — the projection does not
    get to invent a date the store refused to invent.
    """
    try:
        rows = []
        for row in payload.get("conversations") or []:
            if not isinstance(row, dict):
                continue
            rows.append({
                "id": _cell(row.get("id"), 40),
                "source": _cell(row.get("source"), 20),
                "title": _cell(row.get("title"), 120),
                "started_at": row.get("started_at"),
                "model": _cell(row.get("model"), 60),
                "messages": _int_cell(row.get("message_count")),
            })
        stats = payload.get("stats") or {}
        return {"conversations": rows,
                "total": _int_cell(stats.get("conversations")),
                "messages": _int_cell(stats.get("messages")),
                "enabled": bool(payload.get("enabled"))}
    except Exception:  # noqa: BLE001 - a projection never costs the answer
        return payload


def lean_search(payload: Dict[str, Any]) -> Dict[str, Any]:
    """The hits as rows: where the match is and what it says.

    ``tier`` and ``degraded`` are kept deliberately — a coordinating model has
    to be able to SEE that the refined lane was missing.
    """
    try:
        rows = []
        for hit in payload.get("hits") or []:
            if not isinstance(hit, dict):
                continue
            rows.append({
                "conversation_id": _cell(hit.get("conversation_id"), 40),
                "title": _cell(hit.get("title"), 120),
                "source": _cell(hit.get("source"), 20),
                "role": _cell(hit.get("role"), 20),
                "ts": hit.get("ts"),
                "score": hit.get("score"),
                "snippet": _cell(hit.get("snippet"), 400),
            })
        return {"query": _cell(payload.get("query"), 200),
                "hits": rows,
                "tier": _cell(payload.get("tier"), 20),
                "degraded": bool(payload.get("degraded")),
                "candidates": _int_cell(payload.get("candidates"))}
    except Exception:  # noqa: BLE001
        return payload
