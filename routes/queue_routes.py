"""
routes/queue_routes.py — ACT-05: one screen for everything queued or running.

Four systems each already keep their own queue/run bookkeeping — agent turns
(`src.agent_runs`'s lanes), detached shell jobs (`src.bg_jobs`), background
research (`research_handler._active_tasks`), and media renders
(`src.media_runs` + the ComfyUI engine's own `/queue`). Nothing here is a new
store (COMUN rule 4): this module only reads each one through its existing
authority and merges the result into one list, owner-scoped the same way
`GET /api/chat/activity` already scopes agent runs (`_verify_session_owner`).

Position, ETA and reorderability are reported only where the underlying
system actually measures them — a fabricated number would be worse than none:
  * agent_run: a real FIFO position from the run's lane (`_Lane.waiting`),
    and the only kind `POST .../priority` can actually act on, because it is
    the only one of the four with an ordered waiting list of its own to
    reorder.
  * bg_job: "running" or "queued" (RAM-pressure queueing, `src/bg_jobs.py`)
    is a real status; there is no ordered list behind it, so no position.
  * research: no concurrency cap exists in `research_handler`, so every
    active task is already "running" — no queue, no position.
  * media_run: `queued` rows with an engine job id get a live position from
    the ComfyUI engine's own `/queue` (best-effort, short timeout — a
    render's screen already tolerates the engine being briefly unreachable);
    ComfyUI exposes no client-facing reorder call, so priority is not
    supported for this kind either.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request

from core.middleware import require_admin
from routes.session_routes import _verify_session_owner
from src.auth_helpers import effective_user

logger = logging.getLogger(__name__)

#: The four sources this endpoint merges, and the only values `kind` may take
#: in the priority route below.
_KINDS = ("agent_run", "bg_job", "research", "media_run")

#: How long the best-effort live ComfyUI queue-position lookup may take
#: before this endpoint gives up on it for that row (see module docstring).
_MEDIA_POSITION_TIMEOUT_S = 2.0
#: Cap on how many media rows get a live engine lookup per call, so a long
#: render history never turns this endpoint into N synchronous engine calls.
_MEDIA_POSITION_LOOKUPS = 5


def _owned_session(request: Request, session_id: str, session_manager) -> bool:
    try:
        _verify_session_owner(request, session_id, session_manager)
        return True
    except HTTPException:
        return False


def _agent_run_items(request: Request, session_manager) -> List[Dict[str, Any]]:
    from src import agent_runs
    items: List[Dict[str, Any]] = []
    try:
        details = agent_runs.activity_details()
        for sid in agent_runs.active_session_ids():
            if not _owned_session(request, sid, session_manager):
                continue
            snap = details.get(sid)
            if not snap:
                continue
            position = snap.get("queued_position") or 0
            items.append({
                "kind": "agent_run", "id": snap["run_id"], "session_id": sid,
                "label": snap.get("label") or sid,
                "status": "queued" if position else "running",
                "position": position or None,
                "priority": position or None,
                "eta_seconds": None,
                "started_at": snap.get("started_at"),
                "elapsed_s": snap.get("elapsed_s"),
                "reorderable": bool(position),
            })
    except Exception:
        logger.debug("queue: agent_runs listing failed", exc_info=True)
    return items


def _bg_job_items(request: Request, session_manager) -> List[Dict[str, Any]]:
    from src import bg_jobs
    items: List[Dict[str, Any]] = []
    try:
        now = time.time()
        for job in bg_jobs.refresh().values():
            status = job.get("status")
            if status not in ("running", "queued"):
                continue
            sid = job.get("session_id") or ""
            if sid and not _owned_session(request, sid, session_manager):
                continue
            started_at = job.get("started_at")
            items.append({
                "kind": "bg_job", "id": job["id"], "session_id": sid,
                "label": (job.get("command") or "")[:80],
                "status": status,
                "position": None,   # no ordered waiting list behind this status (see module docstring)
                "priority": None,
                "eta_seconds": None,
                "started_at": started_at,
                "elapsed_s": (now - started_at) if started_at else None,
                "reorderable": False,
            })
    except Exception:
        logger.debug("queue: bg_jobs listing failed", exc_info=True)
    return items


def _research_items(owner: Optional[str], research_handler) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    if research_handler is None:
        return items
    try:
        now = time.time()
        for sid, entry in research_handler._active_tasks.items():
            if entry.get("status") != "running":
                continue
            if entry.get("owner", "") != (owner or ""):
                continue
            started_at = entry.get("started_at")
            items.append({
                "kind": "research", "id": sid, "session_id": sid,
                "label": entry.get("query", ""),
                "status": "running",
                "position": None,   # no concurrency cap in research_handler — nothing waits
                "priority": None,
                "eta_seconds": None,
                "started_at": started_at,
                "elapsed_s": (now - started_at) if started_at else None,
                "reorderable": False,
            })
    except Exception:
        logger.debug("queue: research listing failed", exc_info=True)
    return items


def _media_run_active_statuses():
    from src import media_runs
    return {"submitted", "pending", "queued", "running"} | set(media_runs.UNSETTLED_SUBMIT)


async def _media_position(row: Dict[str, Any]) -> Optional[int]:
    """Best-effort, live queue position from the engine for one `queued` row
    (see module docstring — ComfyUI's own `/queue` is the only source of
    truth for this, and this module keeps no copy of it)."""
    if row.get("status") != "queued" or not row.get("engine_job_id"):
        return None
    try:
        from src.media_backends import ComfyUIBackend, ComfyUIError
        backend = ComfyUIBackend(row.get("engine_url") or "", timeout=_MEDIA_POSITION_TIMEOUT_S)
        state = await asyncio.to_thread(backend.status, row["engine_job_id"])
        if state.get("status") == "queued" and "ahead" in state:
            return int(state["ahead"]) + 1
    except Exception:
        logger.debug("queue: media_runs live position lookup failed for %s", row.get("id"), exc_info=True)
    return None


async def _media_run_items(owner: Optional[str]) -> List[Dict[str, Any]]:
    from src import media_runs
    items: List[Dict[str, Any]] = []
    active = _media_run_active_statuses()
    try:
        rows = [r for r in media_runs.recent(owner=owner or "", limit=100) if r["status"] in active]
    except Exception:
        logger.debug("queue: media_runs listing failed", exc_info=True)
        return items
    lookups = 0
    for row in rows:
        position = None
        if lookups < _MEDIA_POSITION_LOOKUPS:
            position = await _media_position(row)
            lookups += 1
        items.append({
            "kind": "media_run", "id": row["id"], "session_id": row.get("session_id") or "",
            "label": row.get("workflow") or row["id"],
            "status": row["status"],
            "position": position,
            "priority": position,
            "eta_seconds": None,
            "started_at": row.get("started_at"),
            "elapsed_s": (time.time() - row["started_at"]) if row.get("started_at") else None,
            "reorderable": False,   # ComfyUI exposes no client-facing reorder call
        })
    return items


def setup_queue_routes(research_handler=None, session_manager=None):
    router = APIRouter(tags=["queue"])

    @router.get("/api/queue")
    async def get_queue(request: Request) -> Dict[str, Any]:
        require_admin(request)
        owner = effective_user(request)
        items: List[Dict[str, Any]] = []
        items.extend(_agent_run_items(request, session_manager))
        items.extend(_bg_job_items(request, session_manager))
        items.extend(_research_items(owner, research_handler))
        items.extend(await _media_run_items(owner))
        # Queued-with-a-known-position first (lowest position first), then
        # everything else (running / not orderable) by recency.
        items.sort(key=lambda it: (
            0 if it["position"] else 1,
            it["position"] or 0,
            -(it["started_at"] or 0),
        ))
        return {"ok": True, "items": items, "ts": time.time()}

    @router.post("/api/queue/{kind}/{item_id}/priority")
    async def set_priority(kind: str, item_id: str, request: Request) -> Dict[str, Any]:
        require_admin(request)
        if kind not in _KINDS:
            raise HTTPException(status_code=404, detail=f"no such queue kind: {kind}")
        if kind != "agent_run":
            # Honest refusal rather than a 200 that changed nothing (see
            # module docstring for why only agent_run has an ordered queue
            # to reorder at all).
            raise HTTPException(status_code=409, detail={
                "reason": "no_ordered_queue",
                "message": f"{kind} has no client-orderable queue to reorder",
            })
        from src import agent_runs
        owned_sid = None
        for sid in agent_runs.active_session_ids():
            if agent_runs.get_run_id(sid) == item_id and _owned_session(request, sid, session_manager):
                owned_sid = sid
                break
        if owned_sid is None:
            raise HTTPException(status_code=404, detail="no such queued agent run for this owner")
        moved = await agent_runs.prioritize_run(item_id)
        if not moved:
            raise HTTPException(status_code=409, detail={
                "reason": "not_waiting",
                "message": "this run is not currently queued (already running, or finished)",
            })
        return {"ok": True, "id": item_id, "position": 1}

    return router
