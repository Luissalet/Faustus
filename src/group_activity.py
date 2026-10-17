"""Observed participant progress, persisted in the existing group step detail."""
from __future__ import annotations

import hashlib
import json
import time


def activity_detail(detail, previous=None, *, status="running", event=False, now=None):
    """A read/lease heartbeat is never model activity; repeated events stay repeated."""
    result = dict(detail or {})
    activity = dict((previous or {}).get("activity") or result.get("activity") or {})
    stamp = time.time() if now is None else now
    if status == "running":
        activity.setdefault("started_at", stamp)
        activity.pop("finished_at", None)
        if activity.get("phase") in ("done", "error", "stopped", "waiting_user"):
            activity["phase"] = "waiting"
        activity.setdefault("phase", "waiting")
    payload = result.get("last_event") or {}
    kind = payload.get("type", "")
    if event and payload and kind not in ("heartbeat", "keepalive", "ping"):
        # Include run identity: a resumed run may restart its sequence counter.
        key = hashlib.sha256(json.dumps([result.get("run_id"), payload], sort_keys=True,
                                       ensure_ascii=False).encode("utf-8")).hexdigest()
        if key != activity.get("event_key"):
            activity.update(last_event_at=stamp, event_key=key,
                            event_count=activity.get("event_count", 0) + 1)
            if kind in ("tool_start", "tool_progress", "tool_call_delta", "tool_calls"):
                activity["phase"] = "tool"
                activity["tool"] = str(payload.get("tool") or payload.get("name") or "")
            elif kind == "tool_output":
                activity["phase"] = "waiting"
                activity.pop("tool", None)
            elif payload.get("thinking"):
                activity["phase"] = "thinking"
                activity.pop("tool", None)
            elif payload.get("delta") or payload.get("response") or kind == "response_replace":
                activity["phase"] = "writing"
                activity.pop("tool", None)
    if status in ("done", "error", "stopped", "waiting_user"):
        activity["phase"] = status
        activity.setdefault("finished_at", stamp)
        activity.pop("tool", None)
    result["activity"] = activity
    return result
