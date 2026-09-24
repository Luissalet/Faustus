"""agent_tools/night_shift_tools.py -- `night_shift`: queue up unattended
work under a budget, and read back a morning report (src/night_shift.py).

One tool, four actions (`action` in the JSON payload):

  start   {"tasks": [...], "workspace", "budget"?: {"max_minutes","max_tasks",
          "max_tokens"?}, "model"?, "verify"?} -> the queued shift
  status  {"id"} -> the shift's current state and per-task results so far
  stop    {"id"} -> ask a running shift to stop after its current task
  report  {"id"?} -> the Markdown report for that shift, or the latest one
          for this owner when `id` is omitted

`start` launches real work in the background (a sequence of `dispatch`
jobs), the same class of action as `delegate_agents` -- see
`src.tool_capabilities.ToolEffect.EXECUTE_CODE`.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


def _args(content: Any) -> Dict[str, Any]:
    raw = (content or "").strip() if isinstance(content, str) else content
    if isinstance(raw, dict):
        return dict(raw)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _owner(ctx: Any) -> str:
    return str((ctx or {}).get("owner") or "")


class NightShiftTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src import night_shift
        args = _args(content)
        action = str(args.get("action") or "start").strip().lower()
        owner = _owner(ctx) or None

        if action == "start":
            spec = dict(args)
            spec.pop("action", None)
            if not spec.get("workspace"):
                from src.tool_execution import get_active_workspace
                spec["workspace"] = get_active_workspace() or ""
            try:
                shift = night_shift.start(owner, spec)
            except ValueError as exc:
                return {"error": str(exc), "exit_code": 1}
            return {
                "output": f"Night shift `{shift['id']}` queued: {len(shift['tasks'])} task(s), "
                          f"budget {shift['budget']['max_minutes']} min / {shift['budget']['max_tasks']} tasks.",
                "shift": shift,
            }

        if action == "status":
            shift_id = str(args.get("id") or "").strip()
            if not shift_id:
                shifts = night_shift.list_for(owner, limit=10)
                return {"output": f"{len(shifts)} shift(s) on record.", "shifts": shifts}
            shift = night_shift.get(owner, shift_id)
            if shift is None:
                return {"error": f"night_shift: no shift found with id {shift_id!r}", "exit_code": 1}
            done = len(shift.get("results") or [])
            return {
                "output": f"Shift `{shift_id}`: {shift.get('state')} ({done}/{len(shift.get('tasks') or [])} tasks run).",
                "shift": shift,
            }

        if action == "stop":
            shift_id = str(args.get("id") or "").strip()
            if not shift_id:
                return {"error": "night_shift: 'id' is required to stop a shift", "exit_code": 1}
            ok = night_shift.stop(owner, shift_id)
            return {
                "output": f"Shift `{shift_id}` will stop after its current task."
                          if ok else f"Shift `{shift_id}` is not running.",
                "stopped": ok,
            }

        if action == "report":
            shift_id = str(args.get("id") or "").strip()
            if not shift_id:
                latest = night_shift.latest_for(owner)
                if latest is None:
                    return {"output": "No night shift has run yet."}
                shift_id = latest["id"]
            return {"output": night_shift.report(owner, shift_id)}

        return {"error": f"night_shift: unknown action {action!r} "
                          "(use start, status, stop or report)", "exit_code": 1}
