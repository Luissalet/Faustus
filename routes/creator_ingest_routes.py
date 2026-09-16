"""Creator ingest routes (WP04), over ``src.creator.ingest``.

Same pattern as ``routes/creator_library_routes.py`` (WP03): every route is
gated by the ``creator_enabled`` setting, checked BEFORE any store access;
owner is always resolved from the authenticated session, never from the
request body (CONTRATO.md rule 3); a foreign owner's job answers exactly
like a missing one, 404.

``POST /api/creator/ingest`` accepts EITHER a ``multipart/form-data`` body
(``project_id`` field + ``file``) for a local file, OR a JSON body
(``{"project_id": ..., "url": ...}``) for a URL — dispatched on the
request's own ``Content-Type``, since FastAPI cannot declare both shapes on
one route. A multipart upload is streamed to a bounded temp file (never
loaded whole into memory) and rejected the instant it exceeds
``creator_ingest_max_bytes``, before the temp file is even completed —
CONTRATO.md rule 6 ("trabajo síncrono largo en asyncio.to_thread") applies
to the CPU-bound validation/copy/proxy work inside ``ingest_file``, run off
the event loop.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile

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


async def _save_bounded(upload, max_bytes: int) -> str:
    """Stream ``upload`` to a temp file, aborting (and deleting the partial
    file) the instant more than ``max_bytes`` have been written — a
    request whose body lies about its size never gets to finish writing."""
    fd, tmp_path = tempfile.mkstemp(prefix=".creator-ingest-")
    written = 0
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    raise HTTPException(
                        status_code=400,
                        detail=f"file exceeds the {max_bytes} byte ingest limit")
                out.write(chunk)
    except HTTPException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise
    return tmp_path


def setup_creator_ingest_routes() -> APIRouter:
    router = APIRouter(prefix="/api/creator/ingest", tags=["creator-ingest"])

    @router.post("")
    async def ingest_route(request: Request):
        owner = _owner(request)
        _require_flag()
        from src.creator import ingest

        content_type = request.headers.get("content-type", "")
        if content_type.startswith("multipart/"):
            form = await request.form()
            project_id = str(form.get("project_id") or "")
            upload = form.get("file")
            if not project_id or upload is None:
                raise HTTPException(400, "project_id and file are required")
            tmp_path = await _save_bounded(upload, ingest.max_ingest_bytes())
            try:
                job = await asyncio.to_thread(
                    ingest.ingest_file, owner, project_id, tmp_path,
                    upload.filename or "upload.bin")
            except ingest.IngestValidationError as exc:
                raise HTTPException(400, str(exc))
            except ValueError as exc:
                raise HTTPException(400, str(exc))
            finally:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_path)
            return job

        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, "a JSON object with project_id and url is required")
        project_id = str(body.get("project_id") or "")
        url = str(body.get("url") or "")
        if not project_id or not url:
            raise HTTPException(400, "project_id and url are required")
        try:
            job = await ingest.ingest_url(owner, project_id, url)
        except ingest.IngestValidationError as exc:
            raise HTTPException(400, str(exc))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return job

    @router.get("/{job_id}")
    def get_ingest_job_route(request: Request, job_id: str):
        owner = _owner(request)
        _require_flag()
        from src.creator import ingest
        job = ingest.get_job(owner, job_id)
        if job is None:
            raise HTTPException(404, "ingest job not found")
        return job

    return router
