"""Owner-scoped execution preferences. They never grant tool authority."""
from __future__ import annotations

from typing import Any, Mapping
import math
import json

FIELDS = frozenset({"autonomy_preset", "reasoning_effort", "thinking_enabled", "initial_response_timeout_s"})
AUTONOMY = frozenset({"supervised", "bounded_autonomous", "read_only"})
EFFORT = frozenset({"auto", "off", "none", "minimal", "low", "medium", "high", "xhigh", "max"})


def validate(values: Any, *, allow_clear: bool = False) -> dict[str, Any]:
    if not isinstance(values, dict):
        raise ValueError("execution_defaults must be an object")
    if set(values) - FIELDS:
        raise ValueError("Unknown execution preference")
    result = {}
    for key, value in values.items():
        if value is None and allow_clear:
            result[key] = None
        elif key == "thinking_enabled":
            if not isinstance(value, bool):
                raise ValueError("thinking_enabled must be a boolean")
            result[key] = value
        elif key == "initial_response_timeout_s":
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 5 <= value <= 900:
                raise ValueError("Initial response wait must be a number between 5 and 900 seconds")
            result[key] = float(value)
        elif not isinstance(value, str) or value not in (AUTONOMY if key == "autonomy_preset" else EFFORT):
            raise ValueError(f"Invalid {key}")
        else:
            result[key] = value
    return result


def patch(saved: Mapping[str, Any], changes: Any) -> dict[str, Any]:
    result = validate(dict(saved))
    for key, value in validate(changes, allow_clear=True).items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = value
    return result


def merge(defaults: Mapping[str, Any], explicit: Mapping[str, Any]) -> dict[str, Any]:
    """Missing or null inherits; explicit false and explicit Auto still win."""
    result = dict(defaults)
    # Existing explicit generation/mode controls remain stronger than project defaults.
    if explicit.get("think_mode") in {"fast", "think", "deep"}:
        result.pop("reasoning_effort", None)
        result.pop("thinking_enabled", None)
    raw = explicit.get("gen_overrides")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            raw = None
    if isinstance(raw, dict):
        if "reasoning_effort" in raw:
            result.pop("reasoning_effort", None)
        if "think" in raw:
            result.pop("thinking_enabled", None)
            result.pop("reasoning_effort", None)
    for key in FIELDS:
        value = explicit.get(key)
        if value is not None and value != "":
            result[key] = value
    return result


def for_session(request, session_id: str, *, verify: bool = True) -> dict[str, Any]:
    from src.auth_helpers import effective_user
    from services.projects import get_store, project_for_session
    owner = effective_user(request)
    # Legacy chats with no configured project keep their current admission path.
    if not verify and not any(row.get("execution_defaults") for row in get_store().list(owner)):
        return {"project_id": None, "defaults": {}, "revision": 0}
    if not session_id:
        return {"project_id": None, "defaults": {}, "revision": 0}
    from routes.session_routes import _verify_session_owner
    _verify_session_owner(request, session_id)
    project = project_for_session(session_id, owner)
    if not project:
        return {"project_id": None, "defaults": {}, "revision": 0}
    return {"project_id": project["id"], "defaults": validate(project.get("execution_defaults") or {}),
            "revision": int(project.get("execution_defaults_revision") or 0)}
