"""agent_tools/goal_tools.py — Goal tools for the agent (WP27).

Four thin executors over ``src.creator.goal``:

    goal_define    write   create a Goal with typed acceptance criteria
    goal_status    read    the goal's current state (or list a project's goals)
    goal_evaluate  write*  runs the REAL checkers and decides done/blocked/etc
    goal_evidence  write*  attach an independently-verified evidence ref

``goal_evaluate`` is the only tool that can ever move a goal to ``done`` —
and even it cannot be talked out of the real checks; it always re-runs every
criterion (``src.creator.goal.evaluate``). ``goal_evidence`` looks like a
write but is deliberately narrow: it calls
``src.creator.goal.verify_manual_evidence`` to INDEPENDENTLY re-check the
claimed ref against the criterion's own real checker before appending
anything, and even a fully verified entry never changes the goal's
``status`` by itself — that is what stops a model from ever marking a goal
done on its own say-so (ORC02: "una autoafirmación... sólo propone cierre").

Gated by the ``creator_enabled`` setting, same discipline every other
Creator tool/route in this repo follows (CONTRATO.md rule 5) — with the flag
off every action here returns an error and nothing is read or written.

Owner is always ``ctx["owner"]`` (the authenticated session's storage
owner, resolved upstream — never taken from the tool's own arguments), the
same pattern ``board_tools.py`` uses.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, Optional

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


def _owner(ctx: dict) -> str:
    return str((ctx or {}).get("owner") or "")


def _creator_enabled() -> bool:
    try:
        from src.settings import get_setting
        return bool(get_setting("creator_enabled", False))
    except Exception:
        return False


def _disabled(tool: str) -> Dict[str, Any]:
    return {
        "error": f"{tool}: creator is not enabled (setting `creator_enabled`)",
        "exit_code": 1, "error_class": "goal.disabled",
    }


def _not_found(tool: str, goal_id: str) -> Dict[str, Any]:
    return {
        "error": f"{tool}: no goal {goal_id!r} for this project/owner",
        "exit_code": 1, "error_class": "goal.not_found",
    }


def _invalid(tool: str, exc: Exception) -> Dict[str, Any]:
    return {"error": f"{tool}: {exc}", "exit_code": 1, "error_class": "goal.invalid"}


class GoalDefineTool:
    """`goal_define`: create a Goal with typed, checkable acceptance
    criteria (test_passes/file_exists/artifact_present/http_ok/
    doc_revision_at_least/custom_check), an optional floor (which criteria
    must pass — defaults to every `required` one) and an optional ceiling
    (max_rounds/max_tokens/max_seconds/max_cost_usd — the hard stop, ADR-13).
    """

    async def execute(self, content: str, ctx: dict) -> dict:
        if not _creator_enabled():
            return _disabled("goal_define")
        owner = _owner(ctx)
        args = _args(content)
        project_id = str(args.get("project_id") or (ctx or {}).get("project_id") or "").strip()
        statement = str(args.get("statement") or "").strip()
        acceptance = args.get("acceptance")
        if not project_id:
            return {"error": "goal_define: project_id is required", "exit_code": 1, "error_class": "goal.invalid"}
        if not statement:
            return {"error": "goal_define: statement is required", "exit_code": 1, "error_class": "goal.invalid"}
        if not isinstance(acceptance, list) or not acceptance:
            return {
                "error": "goal_define: acceptance must be a non-empty list of typed criteria",
                "exit_code": 1, "error_class": "goal.invalid",
            }
        from src.creator import goal as goal_mod
        try:
            goal = goal_mod.get_store().define(
                owner, project_id, statement, acceptance,
                floor=args.get("floor"), ceiling=args.get("ceiling"),
                no_progress_limit=int(args.get("no_progress_limit") or goal_mod.DEFAULT_NO_PROGRESS_LIMIT),
            )
        except goal_mod.InvalidGoal as exc:
            return _invalid("goal_define", exc)
        return {"output": f"goal_define: created {goal.id} ({goal.status})", "exit_code": 0, "goal": goal.to_public_dict()}


class GoalStatusTool:
    """`goal_status`: read one goal's current state (id, status, floor/
    ceiling, usage, evidence, no_progress_streak) or, with only `project_id`,
    list every goal for that project. Read-only — never runs a checker."""

    async def execute(self, content: str, ctx: dict) -> dict:
        if not _creator_enabled():
            return _disabled("goal_status")
        owner = _owner(ctx)
        args = _args(content)
        goal_id = str(args.get("goal_id") or "").strip()
        from src.creator import goal as goal_mod
        store = goal_mod.get_store()
        if goal_id:
            goal = store.get(owner, goal_id)
            if goal is None:
                return _not_found("goal_status", goal_id)
            step = goal_mod.continue_step(goal)
            return {
                "output": f"goal_status: {goal.id} is {goal.status}",
                "exit_code": 0, "goal": goal.to_public_dict(), "continue_step": step,
            }
        project_id = str(args.get("project_id") or (ctx or {}).get("project_id") or "").strip()
        if not project_id:
            return {
                "error": "goal_status: pass goal_id, or project_id to list a project's goals",
                "exit_code": 1, "error_class": "goal.invalid",
            }
        goals = store.list_for_project(owner, project_id)
        return {
            "output": f"goal_status: {len(goals)} goal(s) for project {project_id}",
            "exit_code": 0, "goals": [g.to_public_dict() for g in goals],
        }


class GoalEvaluateTool:
    """`goal_evaluate`: runs every acceptance criterion for real (subprocess,
    filesystem, artifact store, HTTP, another document's revision) and
    decides the goal's next status FROM THAT EVIDENCE ALONE — see
    `src.creator.goal.evaluate` for the exact decision order. This is the
    ONLY tool that can move a goal to `done`, `blocked` or `ceiling_reached`.
    """

    async def execute(self, content: str, ctx: dict) -> dict:
        if not _creator_enabled():
            return _disabled("goal_evaluate")
        owner = _owner(ctx)
        args = _args(content)
        goal_id = str(args.get("goal_id") or "").strip()
        if not goal_id:
            return {"error": "goal_evaluate: goal_id is required", "exit_code": 1, "error_class": "goal.invalid"}
        workspace = args.get("workspace")
        if not workspace:
            try:
                from src.tool_execution import get_active_workspace
                workspace = get_active_workspace()
            except Exception:
                workspace = None
        from src.creator import goal as goal_mod
        store = goal_mod.get_store()
        try:
            report = goal_mod.evaluate(store, owner, goal_id, workspace=workspace)
        except goal_mod.GoalNotFound:
            return _not_found("goal_evaluate", goal_id)
        goal = store.get(owner, goal_id)
        step = goal_mod.continue_step(goal) if goal is not None else {"action": "stop", "reason": "goal vanished"}
        return {
            "output": f"goal_evaluate: {goal_id} -> {report.status} "
                      f"({len(report.met)} met / {len(report.unmet)} unmet)",
            "exit_code": 0, "report": report.to_dict(), "continue_step": step,
        }


class GoalEvidenceTool:
    """`goal_evidence`: attach one evidence ref for a criterion. The ref is
    INDEPENDENTLY re-verified against that criterion's own real checker
    (`src.creator.goal.verify_manual_evidence`) before anything is written;
    a ref that does not check out is refused, nothing is appended, and the
    goal's state does not change. Even a verified entry never flips the
    goal's status by itself — call `goal_evaluate` for that."""

    async def execute(self, content: str, ctx: dict) -> dict:
        if not _creator_enabled():
            return _disabled("goal_evidence")
        owner = _owner(ctx)
        args = _args(content)
        goal_id = str(args.get("goal_id") or "").strip()
        criterion_id = str(args.get("criterion_id") or "").strip()
        ref = str(args.get("ref") or "").strip()
        if not goal_id or not criterion_id or not ref:
            return {
                "error": "goal_evidence: goal_id, criterion_id and ref are all required",
                "exit_code": 1, "error_class": "goal.invalid",
            }
        from src.creator import goal as goal_mod
        store = goal_mod.get_store()
        goal = store.get(owner, goal_id)
        if goal is None:
            return _not_found("goal_evidence", goal_id)
        ok, detail = goal_mod.verify_manual_evidence(goal, criterion_id, ref)
        if not ok:
            return {
                "error": f"goal_evidence: ref could not be independently verified: {detail}",
                "exit_code": 1, "error_class": "goal.unverified_evidence",
            }
        criterion = next((c for c in goal.acceptance if c.id == criterion_id), None)
        entry = goal_mod.EvidenceEntry(
            criterion_id=criterion_id, kind=criterion.kind if criterion else "", ref=ref,
            verified_at=time.time(), by="goal_evidence", ok=True, detail=detail,
        )
        updated = store.append_manual_evidence(owner, goal_id, entry)
        return {
            "output": f"goal_evidence: verified and recorded for {criterion_id}",
            "exit_code": 0, "goal": updated.to_public_dict(),
        }
