"""routes/observations_routes.py - HTTP surface for observations (``src/observations``).

Progressive retrieval, three steps (each answer is far smaller than the next):

GET /api/observations/search?q=&limit=&type=&project=&since=
    -> {"results": [{"id", "created", "type", "title"}]}        (limit <= 20)
GET /api/observations/timeline?anchor=12,S3&before=2&after=2
    -> {"entries": [{"ref", "kind", "created", "type", "title", "anchor"}]}
GET /api/observations/details?ids=12,13
    -> {"records": [full record]}                                (ids <= 5)

Also:

GET /api/observations/recent?limit=&project=&type=              newest first, index shape
GET /api/observations/for-file?path=&workspace=&limit=          notes that touched a file
GET /api/observations/context?project=&max_chars=               the session recap and how it was fitted
GET /api/observations/status                                    counts and the worker's counters
DELETE /api/observations/{id}                                   forget one note

Per-user like the other data routes: a signed-in caller sees only their own
notes; in single-user mode (no username) everything is theirs.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from src.auth_helpers import effective_user, require_user

logger = logging.getLogger(__name__)


def _owner(request: Request) -> str:
    try:
        return effective_user(request) or ""
    except Exception:  # noqa: BLE001
        return ""


def _parse_since(value: Optional[str]) -> Optional[float]:
    """Epoch seconds, or an ISO date / datetime (UTC when it carries no zone)."""
    text = (value or "").strip()
    if not text:
        return None
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return float(text)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(400, "'since' must be epoch seconds or an ISO date")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _refs(raw: str, limit: int) -> List[str]:
    parts = [p.strip() for p in re.split(r"[,\s]+", raw or "") if p.strip()]
    return parts[:limit]


def setup_observations_routes() -> APIRouter:
    router = APIRouter(prefix="/api/observations", tags=["observations"])

    @router.get("/search")
    async def get_search(request: Request, q: str = "", limit: int = Query(20, ge=1, le=20),
                         type: str = "", project: str = "", since: Optional[str] = None,
                         _u: str = Depends(require_user)) -> Dict[str, Any]:
        from src import observations
        results = observations.search(q, limit, owner=_owner(request), project=project, kind=type.strip().lower(),
                                      since=_parse_since(since))
        return {"results": results}

    @router.get("/timeline")
    async def get_timeline(request: Request, anchor: str = "", before: int = Query(2, ge=0, le=10),
                           after: int = Query(2, ge=0, le=10), _u: str = Depends(require_user)) -> Dict[str, Any]:
        from src import observations
        anchors = _refs(anchor, 5)
        if not anchors:
            raise HTTPException(400, "'anchor' needs at least one observation id (or S<n> for a summary)")
        return {"entries": observations.timeline(anchors, before, after, owner=_owner(request))}

    @router.get("/details")
    async def get_details(request: Request, ids: str = "", _u: str = Depends(require_user)) -> Dict[str, Any]:
        from src import observations
        refs = _refs(ids, 50)
        if not refs:
            raise HTTPException(400, "'ids' needs at least one observation id")
        if len(refs) > 5:
            raise HTTPException(400, "at most 5 ids per call; search or use the timeline to narrow down")
        return {"records": observations.details(refs, owner=_owner(request))}

    @router.get("/recent")
    async def get_recent(request: Request, limit: int = Query(20, ge=1, le=100), type: str = "",
                         project: str = "", _u: str = Depends(require_user)) -> Dict[str, Any]:
        from src import observations
        return {"results": observations.recent(limit, owner=_owner(request), project=project, kind=type.strip().lower())}

    @router.get("/for-file")
    async def get_for_file(request: Request, path: str = "", workspace: str = "", limit: int = Query(5, ge=1, le=20),
                           _u: str = Depends(require_user)) -> Dict[str, Any]:
        from src import observations
        if not path.strip():
            raise HTTPException(400, "'path' is required")
        return {"results": observations.observations_for_file(path, limit, owner=_owner(request), workspace=workspace)}

    @router.get("/context")
    async def get_context(request: Request, project: str = "", max_chars: int = Query(6000, ge=200, le=50000),
                          _u: str = Depends(require_user)) -> Dict[str, Any]:
        from src import observations
        return observations.build_context(project, max_chars=max_chars, owner=_owner(request))

    @router.get("/status")
    async def get_status(request: Request, _u: str = Depends(require_user)) -> Dict[str, Any]:
        from src.observations import store, worker
        return {"store": store.stats(_owner(request)), "worker": worker.status()}

    @router.delete("/{obs_id}")
    async def delete_one(request: Request, obs_id: int, _u: str = Depends(require_user)) -> Dict[str, Any]:
        from src.observations import store
        if not store.delete_observation(obs_id, _owner(request)):
            raise HTTPException(404, "No such observation")
        return {"deleted": obs_id}

    return router


__all__ = ["setup_observations_routes"]
