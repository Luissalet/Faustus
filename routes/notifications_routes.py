"""Notification routes (P1 ACT-02): dedupe, per-type/channel prefs, quiet hours.

Nothing in the backend polls for events on its own — lot 49a owns only this
file and cannot touch ``chat_routes``/``agent_loop`` to hook server-side
emission. Instead ``POST /api/notifications/emit`` is idempotent per
``dedupe_key``: the shell (studio/src/shell/notifications.ts) calls it the
moment it *observes* a new pending approval/question/finished task through
the polling it already does (Activity's poller, `/api/activity`), and calling
it again for the same key on a reconnect is a no-op that returns the existing
row rather than creating a duplicate or re-flipping it back to unread. That is
the whole fix for "a reconnect doesn't re-notify the same pending permission
twenty times" (ACT-02's acceptance line): the *sending* side may fire twenty
times, the *stored* notification does not.

Preferences are per owner, per notification type, with independent in-app and
desktop channels, plus one quiet-hours window that only ever suppresses the
desktop channel — the in-app tray always keeps every event so nothing is lost
to a bad clock, only deferred from popping onto the screen.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from core.middleware import require_admin
from src.auth_helpers import effective_user
from src.constants import DATA_DIR

logger = logging.getLogger(__name__)

NOTIFICATIONS_FILE = os.path.join(DATA_DIR, "notifications.json")
NOTIFICATION_TYPES = ("approval", "question", "task_done", "task_failed", "blocked")
_MAX_EVENTS_PER_OWNER = 300
_lock = threading.RLock()

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_SECRET_LIKE = re.compile(r"[A-Za-z0-9_\-]{24,}")


def _scrub(text: str) -> str:
    """Same defense-in-depth redaction as prompts_routes._scrub: a token-shaped
    run of 24+ alnum/`_`/`-` characters never reaches a stored notification,
    which is what a lock-screen preview would show verbatim."""
    return _SECRET_LIKE.sub("[redacted]", text or "")


def _default_prefs() -> Dict[str, Any]:
    return {
        "channels": {"in_app": True, "desktop": True},
        "types": {t: {"enabled": True, "channels": ["in_app", "desktop"]} for t in NOTIFICATION_TYPES},
        "quiet_hours": {"enabled": False, "start": "22:00", "end": "07:00"},
    }


def _load() -> Dict[str, Any]:
    if not os.path.exists(NOTIFICATIONS_FILE):
        return {"prefs": {}, "events": {}}
    try:
        with open(NOTIFICATIONS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            logger.error("notifications.json malformed (not an object); treating as empty")
            return {"prefs": {}, "events": {}}
        data.setdefault("prefs", {})
        data.setdefault("events", {})
        return data
    except Exception as e:
        logger.error("Failed to load notifications.json: %s", e)
        return {"prefs": {}, "events": {}}


def _save(data: Dict[str, Any]) -> None:
    from core.atomic_io import atomic_write_json
    atomic_write_json(NOTIFICATIONS_FILE, data, indent=2)


def _prefs_for(data: Dict[str, Any], owner: str) -> Dict[str, Any]:
    stored = data["prefs"].get(owner)
    defaults = _default_prefs()
    if not isinstance(stored, dict):
        return defaults
    merged = defaults
    merged["channels"].update({k: v for k, v in (stored.get("channels") or {}).items() if k in merged["channels"]})
    for t, cfg in (stored.get("types") or {}).items():
        if t in merged["types"] and isinstance(cfg, dict):
            merged["types"][t]["enabled"] = bool(cfg.get("enabled", True))
            chans = [c for c in cfg.get("channels", ["in_app", "desktop"]) if c in ("in_app", "desktop")]
            merged["types"][t]["channels"] = chans or []
    qh = stored.get("quiet_hours") or {}
    if isinstance(qh, dict):
        merged["quiet_hours"]["enabled"] = bool(qh.get("enabled", False))
        if _TIME_RE.match(str(qh.get("start", ""))):
            merged["quiet_hours"]["start"] = qh["start"]
        if _TIME_RE.match(str(qh.get("end", ""))):
            merged["quiet_hours"]["end"] = qh["end"]
    return merged


def _in_quiet_hours(quiet: Dict[str, Any], now_local: time.struct_time) -> bool:
    if not quiet.get("enabled"):
        return False
    start_h, start_m = (int(x) for x in quiet["start"].split(":"))
    end_h, end_m = (int(x) for x in quiet["end"].split(":"))
    start = start_h * 60 + start_m
    end = end_h * 60 + end_m
    now = now_local.tm_hour * 60 + now_local.tm_min
    if start == end:
        return False
    if start < end:
        return start <= now < end
    return now >= start or now < end  # wraps past midnight


class PrefsIn(BaseModel):
    channels: Dict[str, bool] = Field(default_factory=dict)
    types: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    quiet_hours: Dict[str, Any] = Field(default_factory=dict)


class EmitIn(BaseModel):
    type: str = Field(...)
    dedupe_key: str = Field(..., min_length=1, max_length=300)
    title: str = Field(..., min_length=1, max_length=200)
    detail: str = Field("", max_length=2000)
    screen: str = Field("activity", max_length=100)
    target: str = Field("", max_length=300)

    @field_validator("type")
    @classmethod
    def _valid_type(cls, v: str) -> str:
        if v not in NOTIFICATION_TYPES:
            raise ValueError(f"Unknown notification type: {v}. Use one of {NOTIFICATION_TYPES}.")
        return v


def setup_notification_routes() -> APIRouter:
    router = APIRouter(prefix="/api/notifications", tags=["notifications"])

    @router.get("/prefs")
    async def get_prefs(request: Request, _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        owner = effective_user(request) or ""
        with _lock:
            return _prefs_for(_load(), owner)

    @router.put("/prefs")
    async def put_prefs(payload: PrefsIn, request: Request, _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        owner = effective_user(request) or ""
        with _lock:
            data = _load()
            current = _prefs_for(data, owner)
            current["channels"].update({k: bool(v) for k, v in payload.channels.items() if k in current["channels"]})
            for t, cfg in payload.types.items():
                if t not in current["types"] or not isinstance(cfg, dict):
                    continue
                current["types"][t]["enabled"] = bool(cfg.get("enabled", current["types"][t]["enabled"]))
                if "channels" in cfg:
                    chans = [c for c in cfg["channels"] if c in ("in_app", "desktop")]
                    current["types"][t]["channels"] = chans
            qh = payload.quiet_hours
            if qh:
                if "enabled" in qh:
                    current["quiet_hours"]["enabled"] = bool(qh["enabled"])
                for key in ("start", "end"):
                    if key in qh and _TIME_RE.match(str(qh[key])):
                        current["quiet_hours"][key] = qh[key]
                    elif key in qh:
                        raise HTTPException(400, f"quiet_hours.{key} must be HH:MM")
            data["prefs"][owner] = current
            _save(data)
        return current

    @router.get("")
    async def list_notifications(request: Request, unread_only: bool = False,
                                  _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        owner = effective_user(request) or ""
        with _lock:
            rows = list(_load()["events"].get(owner, []))
        if unread_only:
            rows = [r for r in rows if not r.get("read")]
        rows = sorted(rows, key=lambda r: r.get("created_at", ""), reverse=True)
        return {"notifications": rows, "unread": sum(1 for r in rows if not r.get("read"))}

    @router.post("/emit")
    async def emit_notification(payload: EmitIn, request: Request,
                                 _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        owner = effective_user(request) or ""
        with _lock:
            data = _load()
            prefs = _prefs_for(data, owner)
            type_cfg = prefs["types"].get(payload.type, {"enabled": True, "channels": ["in_app", "desktop"]})
            if not type_cfg.get("enabled", True):
                return {"stored": False, "reason": "type_disabled"}
            events = data["events"].setdefault(owner, [])
            existing = next((e for e in events if e.get("dedupe_key") == payload.dedupe_key), None)
            if existing:
                # Idempotent: a reconnect that re-observes the same pending
                # item must not re-alert or resurrect a dismissed/read row.
                return {"stored": True, "deduped": True, "notification": existing}
            desktop_wanted = "desktop" in type_cfg.get("channels", []) and prefs["channels"].get("desktop", True)
            suppressed_quiet = desktop_wanted and _in_quiet_hours(prefs["quiet_hours"], time.localtime())
            row = {
                "id": uuid.uuid4().hex,
                "type": payload.type,
                "dedupe_key": payload.dedupe_key,
                "title": _scrub(payload.title)[:200],
                "detail": _scrub(payload.detail)[:2000],
                "target": {"screen": payload.screen, "run": payload.target},
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "read": False,
                "desktop_allowed": desktop_wanted and not suppressed_quiet,
                "suppressed_quiet_hours": suppressed_quiet,
            }
            events.append(row)
            if len(events) > _MAX_EVENTS_PER_OWNER:
                data["events"][owner] = events[-_MAX_EVENTS_PER_OWNER:]
            _save(data)
        return {"stored": True, "deduped": False, "notification": row}

    @router.post("/{notification_id}/read")
    async def mark_read(notification_id: str, request: Request, _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        owner = effective_user(request) or ""
        with _lock:
            data = _load()
            events = data["events"].get(owner, [])
            row = next((e for e in events if e["id"] == notification_id), None)
            if not row:
                raise HTTPException(404, "Notification not found")
            row["read"] = True
            _save(data)
        return {"ok": True}

    @router.post("/read-all")
    async def mark_all_read(request: Request, _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        owner = effective_user(request) or ""
        with _lock:
            data = _load()
            events = data["events"].get(owner, [])
            for e in events:
                e["read"] = True
            _save(data)
        return {"ok": True, "count": len(events)}

    @router.delete("/{notification_id}")
    async def dismiss(notification_id: str, request: Request, _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        owner = effective_user(request) or ""
        with _lock:
            data = _load()
            events = data["events"].get(owner, [])
            before = len(events)
            data["events"][owner] = [e for e in events if e["id"] != notification_id]
            if len(data["events"][owner]) == before:
                raise HTTPException(404, "Notification not found")
            _save(data)
        return {"ok": True}

    return router
