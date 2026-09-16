"""routes/creator_resources_routes.py — WP30: HTTP adapter over
`src/creator/resources.py` (physical inventory + the conservative
admission gate).

Same discipline as `routes/creator_routes.py`: the ``creator_enabled``
setting is checked BEFORE any store access, so with the flag off every
route below answers 404 and nothing is read (CONTRATO.md rule 5). Owner is
resolved from the authenticated session, never from the request (rule 3) —
though this route reads no per-owner data at all; it names PHYSICAL devices
and in-process admissions, which are process-wide by nature (see
`src/resource_admission.py`'s own docstring on why a lease has no owner
scoping today).

NOT YET WIRED into ``app.py`` — out of this lot's file list; the exact
``app.include_router(...)`` line is in this lot's report and
``WP30_wiring.md``.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from src.auth_helpers import require_user


def _creator_enabled() -> bool:
    from src.settings import get_setting
    return bool(get_setting("creator_enabled", False))


def _require_flag() -> None:
    if not _creator_enabled():
        raise HTTPException(status_code=404, detail="creator is not enabled")


def setup_creator_resources_routes() -> APIRouter:
    router = APIRouter(prefix="/api/creator", tags=["creator"])

    @router.get("/resources")
    def get_resources(request: Request):
        """Physical inventory (deduplicated GPUs, RAM, disk, CPU), the
        active resource mode, every admission currently held, and — as
        `queue` — every media run parked `queued` with a `resource_wait`
        reason because it could not be admitted yet."""
        require_user(request)
        _require_flag()
        from src.creator import resources as creator_resources

        inventory = creator_resources.device_inventory()
        active = creator_resources.active_admissions()
        queue = _resource_wait_queue()
        return {
            "creator_enabled": True,
            "inventory": inventory,
            "active_admissions": active,
            "queue": queue,
        }

    return router


def _resource_wait_queue() -> list:
    """Runs parked `queued` by WP30's own gate (never sent to an engine),
    read straight off `media_runs` rows rather than a separate authority —
    `media_runs.py::start()` is where that reason string is written (see
    its WP30 admission comment)."""
    try:
        from core.database import MediaRunRow, SessionLocal
    except Exception:  # noqa: BLE001
        return []
    out = []
    db = SessionLocal()
    try:
        rows = (db.query(MediaRunRow)
                .filter(MediaRunRow.status == "queued", MediaRunRow.engine_job_id.is_(None))
                .order_by(MediaRunRow.created_at_iso.asc()).limit(100).all())
        for row in rows:
            reason = row.reason or ""
            if not reason.startswith("resource_wait:"):
                continue
            out.append({
                "run_id": row.id, "workflow_id": row.workflow_id,
                "workflow_version": row.workflow_version,
                "engine_url": row.engine_url, "owner": row.owner or "",
                "reason": reason, "created_at": row.created_at_iso,
            })
    except Exception:  # noqa: BLE001 - a DB not yet migrated/reachable is an empty queue, not a 500
        return []
    finally:
        db.close()
    return out


__all__ = ["setup_creator_resources_routes"]
