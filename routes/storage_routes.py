"""Disk ballast admin routes — /api/storage/* (FAUSTUS).

The human end of ``src/disk_ballast.py``: read how tight the disk is and what
the scorer thinks could go, allocate or release ballast, move a candidate into
quarantine, and undo that.

Admin-only, all of it. The read names paths under ``DATA_DIR`` and the writes
move real files, so this is an authority boundary in both directions — and the
write that matters, ``/quarantine``, is refused outright while
``agent_disk_ballast`` is ``observe`` (the default). This module never deletes
anything: quarantine MOVES, ``/undo`` moves it back, and the only destructive
operation in the feature (the 24-hour sweep) is not exposed here at all.

``GET /api/storage/status`` also answers in robot mode (``?robot=1`` /
``?format=toon``, src/robot_envelope.py) for a coordinator watching a machine
it cannot see. It sends the payload as it stands rather than a lean projection
(src/robot_projection.py): the expensive part of this read is the candidate
list, which is already flat scalar rows — exactly the tabular shape TOON pays
for — and projecting it would throw away the ``reasons`` and ``vetoes`` that
are the only reason to look at it. A call without those query parameters
answers exactly as it always did.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from core.middleware import require_admin
from src import robot_envelope as robot

logger = logging.getLogger(__name__)


class BallastBody(BaseModel):
    count: Optional[int] = None
    size_bytes: Optional[int] = None


class ReleaseBody(BaseModel):
    n: int = 1


class QuarantineBody(BaseModel):
    path: str
    reason: str = ""


class UndoBody(BaseModel):
    id: str


def setup_storage_routes() -> APIRouter:
    router = APIRouter(prefix="/api/storage", tags=["storage"])

    @router.get("/status")
    async def storage_status(
        request: Request,
        limit: int = 200,
        _admin: None = Depends(require_admin),
    ) -> Dict[str, Any]:
        """Free space, urgency, ballast, quarantine and the scored candidates.

        Pure measurement in every mode: this call allocates nothing and moves
        nothing.
        """
        from src import disk_ballast

        def payload() -> Dict[str, Any]:
            return {"status": "success", **disk_ballast.status(limit=limit)}

        if robot.wants(request):
            return await robot.reply(request, payload)
        return payload()

    @router.post("/ballast")
    async def allocate_ballast(body: Optional[BallastBody] = None,
                               _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        """Bring the ballast up to ``count`` files.

        Refused — with the reason in the answer, not as an error — when the
        allocation would leave the volume under its floor. The ballast must
        never fill the disk it protects.
        """
        from src import disk_ballast
        count = body.count if body else None
        size = body.size_bytes if body else None
        if count is not None and count < 0:
            raise HTTPException(status_code=400, detail="count must be >= 0")
        if size is not None and size <= 0:
            raise HTTPException(status_code=400, detail="size_bytes must be > 0")
        return {"status": "success", **disk_ballast.ensure(count, size)}

    @router.post("/release")
    async def release_ballast(body: Optional[ReleaseBody] = None,
                              _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        """Unlink n ballast files — instant headroom, and the whole point."""
        from src import disk_ballast
        n = body.n if body else 1
        if n < 0:
            raise HTTPException(status_code=400, detail="n must be >= 0")
        return {"status": "success", **disk_ballast.release(n)}

    @router.post("/quarantine")
    async def quarantine_path(body: QuarantineBody,
                              _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        """MOVE one candidate to the quarantine. Never a delete.

        Answers 200 with ``ok: false`` and the reason when the mode forbids it
        (``observe``), the canary budget is spent, or a veto applies — a
        refusal is information, not a server error.
        """
        from src import disk_ballast
        if not (body.path or "").strip():
            raise HTTPException(status_code=400, detail="path is required")
        return {"status": "success", **disk_ballast.quarantine(body.path,
                                                               reason=body.reason)}

    @router.post("/undo")
    async def undo_quarantine(body: UndoBody,
                              _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        """Put a quarantined entry back where it came from."""
        from src import disk_ballast
        if not (body.id or "").strip():
            raise HTTPException(status_code=400, detail="id is required")
        result = disk_ballast.undo(body.id)
        if not result.get("ok") and result.get("reason") == "no such quarantine entry":
            raise HTTPException(status_code=404, detail="no such quarantine entry")
        return {"status": "success", **result}

    return router
