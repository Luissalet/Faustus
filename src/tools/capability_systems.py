"""Agent-facing adapters for Teach, Immune and Branching.

The current project is always derived from the chat session.  Model-supplied
project/owner fields are ignored so these tools cannot widen their own scope.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional, Tuple

from services.projects import project_for_session
from src.settings import get_setting


def _args(content: str) -> Dict[str, Any]:
    value = json.loads(content or "{}")
    if not isinstance(value, dict):
        raise ValueError("arguments must be a JSON object")
    return value


def _project(session_id: str, owner: str) -> Tuple[str, Dict[str, Any]]:
    project = project_for_session(session_id or "", owner)
    return str((project or {}).get("id") or ""), project or {}


async def do_manage_teach_mode(content: str, *, session_id: str, owner: str) -> Dict[str, Any]:
    if not get_setting("agent_teach_mode", False):
        return {"error": "Modo Enséñame is disabled in settings", "exit_code": 1}
    try:
        args = _args(content)
        action = str(args.get("action") or "status").strip().lower()
        from src.teach_mode.service import service
        api = service()
        project_id, _ = _project(session_id, owner)
        active = api.active(owner=owner, session_id=session_id, project_id=project_id)
        if action == "status":
            return {"action": action, "active_demonstration": active,
                    "message": "Recording is active" if active else "No demonstration is recording"}
        if action == "start":
            if active:
                return {"error": "A demonstration is already recording in this chat",
                        "demonstration": active, "exit_code": 1}
            row = api.start(owner=owner, project_id=project_id, session_id=session_id,
                            request={"title": args.get("title") or "Taught procedure",
                                     "intent": args.get("intent") or args.get("title") or "Taught procedure",
                                     "type": args.get("type") or "tool_native",
                                     "source_refs": args.get("source_refs") or [],
                                     "target_refs": args.get("target_refs") or [],
                                     "completion_mode": args.get("completion_mode") or "professional"})
            return {"action": action, "demonstration": row,
                    "message": "Recording started; subsequent tool calls in this chat will be captured"}
        demonstration_id = str(args.get("demonstration_id") or (active or {}).get("id") or "")
        if action in ("pause", "resume"):
            if not demonstration_id:
                return {"error": "demonstration_id is required", "exit_code": 1}
            row = getattr(api, action)(owner=owner, demonstration_id=demonstration_id)
            return {"action": action, "demonstration": row}
        if action in ("stop", "cancel"):
            if not demonstration_id:
                return {"error": "No active demonstration", "exit_code": 1}
            row = api.stop(owner=owner, demonstration_id=demonstration_id, cancelled=action == "cancel")
            return {"action": action, "demonstration": row}
        if action == "compile":
            if not demonstration_id:
                return {"error": "demonstration_id is required", "exit_code": 1}
            row = api.compile(owner=owner, demonstration_id=demonstration_id)
            return {"action": action, "procedure": row,
                    "message": "Candidate compiled; simulate and validate it before human approval"}
        procedure_id = str(args.get("procedure_id") or "")
        if action in ("simulate", "validate"):
            if not procedure_id:
                return {"error": "procedure_id is required", "exit_code": 1}
            evidence = args.get("evidence") or {}
            row = api.transition(owner=owner, procedure_id=procedure_id, action=action,
                                 evidence=evidence)
            return {"action": action, "procedure": row}
        if action in ("approve", "install", "establish", "revoke"):
            return {"error": "This transition requires the human-only API; an agent cannot approve or install its own learned procedure",
                    "exit_code": 1, "requires_human": True}
        return {"error": "Action must be status, start, pause, resume, stop, cancel, compile, simulate or validate",
                "exit_code": 1}
    except Exception as exc:
        return {"error": str(exc), "exit_code": 1}


async def do_capability_health(content: str, *, session_id: str, owner: str) -> Dict[str, Any]:
    try:
        args = _args(content)
        action = str(args.get("action") or "get").strip().lower()
        from src.immune_system.service import service
        api = service()
        project_id, _ = _project(session_id, owner)
        asset_id = str(args.get("asset_id") or "")
        if action in ("get", "list"):
            if asset_id:
                return {"action": "get", "asset": api.asset(owner=owner, asset_id=asset_id),
                        "enabled": bool(get_setting("agent_immune_system", False))}
            return {"action": "list", "assets": api.assets(owner=owner, project_id=project_id,
                                                               status=str(args.get("status") or "")),
                    "enabled": bool(get_setting("agent_immune_system", False))}
        if not get_setting("agent_immune_system", False):
            return {"error": "Immune System is disabled in settings", "exit_code": 1}
        if action == "register":
            request = dict(args.get("asset") or {})
            request["project_id"] = project_id
            return {"action": action, "asset": api.register_asset(owner=owner, request=request)}
        if action == "assess":
            return {"action": action, "asset": api.assess(owner=owner, asset_id=asset_id,
                                                            assessment=args.get("assessment") or {})}
        if action == "report_failure":
            return {"action": action, "incident": api.report_failure(
                owner=owner, asset_id=asset_id, failure=args.get("failure") or {})}
        return {"error": "Action must be get, list, register, assess or report_failure", "exit_code": 1}
    except Exception as exc:
        return {"error": str(exc), "exit_code": 1}


async def do_branch_futures(content: str, *, session_id: str, owner: str) -> Dict[str, Any]:
    try:
        args = _args(content)
        action = str(args.get("action") or "get").strip().lower()
        from src.branching_futures.service import service
        api = service()
        project_id, _ = _project(session_id, owner)
        future_id = str(args.get("future_id") or "")
        if action in ("get", "list"):
            if future_id:
                return {"action": "get", "future": api.future(owner=owner, future_id=future_id),
                        "enabled": bool(get_setting("agent_branching_futures", False))}
            return {"action": "list", "futures": api.futures(owner=owner, project_id=project_id),
                    "enabled": bool(get_setting("agent_branching_futures", False))}
        if not get_setting("agent_branching_futures", False):
            return {"error": "Branching Futures is disabled in settings", "exit_code": 1}
        if action == "create":
            request = dict(args.get("future") or {})
            request["project_id"] = project_id
            request["session_id"] = session_id
            return {"action": action, "future": api.create(owner=owner, request=request,
                                                              project_id=project_id, session_id=session_id)}
        if action == "start_branch":
            return {"action": action, "branch": api.start_branch(owner=owner, future_id=future_id,
                                                                   branch_id=str(args.get("branch_id") or ""))}
        if action == "submit_result":
            return {"action": action, "result": api.submit_result(owner=owner, future_id=future_id,
                                                                    branch_id=str(args.get("branch_id") or ""),
                                                                    result=args.get("result") or {})}
        if action == "evaluate":
            return {"action": action, "evaluation": api.evaluate(owner=owner, future_id=future_id)}
        return {"error": "Action must be get, list, create, start_branch, submit_result or evaluate",
                "exit_code": 1}
    except Exception as exc:
        return {"error": str(exc), "exit_code": 1}
