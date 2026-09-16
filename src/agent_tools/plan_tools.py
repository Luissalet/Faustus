"""P1 tools: `plan_status`, `plan_task`, `plan_done`, `plan_skip`, `plan_next`.

Thin executors over `src.plan_tracker` — the persisted, per-project plan
state a `=== File/ZIP: ... ===` attachment was parsed into once
(`upsert_from_attachment`), so a model can ask "what's the current task /
what does it need" instead of the harness reinjecting the whole attachment
body every chat (see `src/plan_tracker.py`'s module docstring).

Scope/hash resolution mirrors `todowrite` (src/agent_tools/coding_tools.py):
`ctx["project_id"]` when present, else `get_active_workspace()`
(src/tool_execution.py). No active plan for that scope is reported as an
ordinary tool error, not an exception.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

from src import plan_tracker as pt


def _scope_from_ctx(ctx: Dict[str, Any]) -> str:
    project_id = str((ctx or {}).get("project_id") or "").strip()
    workspace: Optional[str] = None
    if not project_id:
        try:
            from src.tool_execution import get_active_workspace
            workspace = get_active_workspace()
        except Exception:
            workspace = None
    return pt.scope_for(project_id or None, workspace)


def _active_tracker(ctx: Dict[str, Any]):
    scope = _scope_from_ctx(ctx)
    tracker = pt.active(scope)
    return scope, tracker


def _no_active_plan() -> Dict[str, Any]:
    return {"error": "no active plan for this project", "exit_code": 1}


def _parse_args(content: str) -> Optional[Dict[str, Any]]:
    text = (content or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _compact_task_line(task: Dict[str, Any], status: str) -> str:
    return f'{task.get("id")} {task.get("key")} {status} {task.get("title", "")}'


class PlanStatusTool:
    """No args (or `{}`): overall progress plus a compact task list."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _parse_args(content)
        if args is None:
            return {"error": "plan_status: JSON object required", "exit_code": 1}
        scope, tracker = _active_tracker(ctx)
        if not tracker:
            return _no_active_plan()
        prog = pt.progress(tracker)
        state = tracker.get("state", {})
        lines = [
            _compact_task_line(t, state.get(t["id"], {}).get("status", "pending"))
            for t in tracker.get("tasks", [])
        ]
        return {
            "output": (
                f'{tracker.get("title", "")} ({tracker.get("hash", "")}): '
                f'{prog["done"]}/{prog["total"]} done\n' + "\n".join(lines)
            ),
            "exit_code": 0,
            "progress": prog,
            "tasks": lines,
        }


class PlanTaskTool:
    """`{"id": "t03"}` (id or key): full task text, acceptance and files."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _parse_args(content)
        if args is None:
            return {"error": "plan_task: JSON object required", "exit_code": 1}
        ident = str(args.get("id") or args.get("key") or "").strip()
        scope, tracker = _active_tracker(ctx)
        if not tracker:
            return _no_active_plan()
        if not ident:
            cur = pt.current_task(tracker)
            if not cur:
                return {"error": "plan_task: no task id/key given and no current task", "exit_code": 1}
            task = cur
        else:
            task = pt.find_task(tracker, ident)
            if not task:
                return {"error": f"plan_task: no such task {ident!r}", "exit_code": 1}
        status = tracker.get("state", {}).get(task["id"], {}).get("status", "pending")
        out = [
            f'{task.get("id")} {task.get("key")} [{status}] {task.get("title", "")}',
            "",
            task.get("text", ""),
        ]
        if task.get("acceptance"):
            out.append("")
            out.append("Acceptance:")
            out.extend(f"- {a}" for a in task["acceptance"])
        if task.get("files"):
            out.append("")
            out.append("Files: " + ", ".join(task["files"]))
        return {
            "output": "\n".join(out),
            "exit_code": 0,
            "task": task,
            "status": status,
        }


class PlanDoneTool:
    """`{"id", "evidence"}` — evidence required, >= 20 chars."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _parse_args(content)
        if args is None:
            return {"error": "plan_done: JSON object required", "exit_code": 1}
        ident = str(args.get("id") or args.get("key") or "").strip()
        evidence = str(args.get("evidence") or "").strip()
        if not ident:
            return {"error": "plan_done: 'id' required", "exit_code": 1}
        if len(evidence) < 20:
            return {"error": "plan_done: 'evidence' required (>= 20 chars)", "exit_code": 1}
        scope, tracker = _active_tracker(ctx)
        if not tracker:
            return _no_active_plan()
        task = pt.find_task(tracker, ident)
        if not task:
            return {"error": f"plan_done: no such task {ident!r}", "exit_code": 1}
        turn = (ctx or {}).get("turn")
        updated = pt.mark(scope, tracker["hash"], task["id"], "done", evidence, turn)
        if not updated:
            return {"error": "plan_done: failed to persist", "exit_code": 1}
        result: Dict[str, Any] = {
            "output": f'{task.get("id")} {task.get("key")} marked done',
            "exit_code": 0,
            "progress": pt.progress(updated),
        }
        ledger_mutations = (ctx or {}).get("ledger_mutations")
        files = task.get("files") or []
        if isinstance(ledger_mutations, (list, tuple, set)) and files:
            mutated = {str(p) for p in ledger_mutations}
            unverified = [f for f in files if f not in mutated]
            if unverified:
                result["unverified_files"] = unverified
        return result


class PlanSkipTool:
    """`{"id", "reason"}` — reason required."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _parse_args(content)
        if args is None:
            return {"error": "plan_skip: JSON object required", "exit_code": 1}
        ident = str(args.get("id") or args.get("key") or "").strip()
        reason = str(args.get("reason") or "").strip()
        if not ident:
            return {"error": "plan_skip: 'id' required", "exit_code": 1}
        if not reason:
            return {"error": "plan_skip: 'reason' required", "exit_code": 1}
        scope, tracker = _active_tracker(ctx)
        if not tracker:
            return _no_active_plan()
        task = pt.find_task(tracker, ident)
        if not task:
            return {"error": f"plan_skip: no such task {ident!r}", "exit_code": 1}
        turn = (ctx or {}).get("turn")
        updated = pt.mark(scope, tracker["hash"], task["id"], "skipped", reason, turn)
        if not updated:
            return {"error": "plan_skip: failed to persist", "exit_code": 1}
        return {
            "output": f'{task.get("id")} {task.get("key")} skipped: {reason}',
            "exit_code": 0,
            "progress": pt.progress(updated),
        }


class PlanNextTool:
    """`{}`: does NOT mark the current task done — only returns the next
    `pending` task and puts it `in_progress`."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _parse_args(content)
        if args is None:
            return {"error": "plan_next: JSON object required", "exit_code": 1}
        scope, tracker = _active_tracker(ctx)
        if not tracker:
            return _no_active_plan()
        nxt = pt.current_task(tracker)
        if not nxt:
            return {
                "output": "no pending tasks left",
                "exit_code": 0,
                "progress": pt.progress(tracker),
            }
        status = tracker.get("state", {}).get(nxt["id"], {}).get("status", "pending")
        if status != "in_progress":
            turn = (ctx or {}).get("turn")
            updated = pt.mark(scope, tracker["hash"], nxt["id"], "in_progress", "", turn)
            if updated:
                tracker = updated
        return {
            "output": f'{nxt.get("id")} {nxt.get("key")} — {nxt.get("title", "")}',
            "exit_code": 0,
            "task": nxt,
            "progress": pt.progress(tracker),
        }
