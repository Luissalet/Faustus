# routes/meetings_routes.py
"""Meeting notes API routes (FEATURE B) — POST /api/meetings uploads an audio
file and runs the record → transcript → notes pipeline as a background job;
GET /api/meetings and GET /api/meetings/{id} read the finished notes back.

Owner-scoped the same way `routes/research/research_routes.py` scopes
research: `src.auth_helpers.require_user` resolves the caller, and every
read filters on the `owner` field stamped into the sidecar/job at creation
time, so one account never sees another's meeting notes.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Form, HTTPException, Request, UploadFile, File
from starlette.concurrency import run_in_threadpool

from src.auth_helpers import require_user
from src.upload_limits import MEETINGS_MAX_AUDIO_BYTES, read_upload_limited

logger = logging.getLogger(__name__)


def setup_meetings_routes() -> APIRouter:
    router = APIRouter(prefix="/api/meetings", tags=["meetings"])

    @router.post("")
    async def upload_meeting(
        request: Request,
        file: UploadFile = File(...),
        title: str = Form(""),
        language: str = Form("", max_length=20, pattern=r"^[a-zA-Z-]*$"),
        project_id: str = Form(""),
    ):
        user = require_user(request)
        audio_bytes = await read_upload_limited(file, MEETINGS_MAX_AUDIO_BYTES, "Audio file")
        if not audio_bytes:
            raise HTTPException(status_code=400, detail={"message": "Empty audio file"})

        from src import meetings

        job_id = await run_in_threadpool(
            meetings.create_job,
            audio_bytes,
            file.filename or "meeting",
            title=title,
            language=language,
            project_id=project_id or None,
            owner=user,
        )
        return {"job_id": job_id, "status": "queued"}

    @router.get("/jobs/{job_id}")
    async def meeting_job_status(job_id: str, request: Request):
        user = require_user(request)
        from src import meetings

        job = await run_in_threadpool(meetings.get_job, job_id, owner=user)
        if job is None:
            raise HTTPException(status_code=404, detail={"message": "Job not found"})
        return job

    @router.get("")
    async def list_meetings(request: Request, limit: int = 100):
        user = require_user(request)
        from src import meetings

        limit = max(1, min(int(limit or 100), 500))
        items = await run_in_threadpool(meetings.list_meetings, owner=user, limit=limit)
        return {"meetings": items}

    @router.get("/{meeting_id}")
    async def get_meeting(meeting_id: str, request: Request):
        user = require_user(request)
        from src import meetings

        item = await run_in_threadpool(meetings.get_meeting, meeting_id, owner=user)
        if item is None:
            raise HTTPException(status_code=404, detail={"message": "Meeting not found"})
        return item

    return router
