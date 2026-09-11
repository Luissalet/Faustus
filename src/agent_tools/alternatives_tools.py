"""agent_tools/alternatives_tools.py — CMP-13 agent tools (W2-G).

Three thin executors over `src.alternatives`, the same shape `git_tools.py`
gives git and `board_tools.py` gives the board: the agent gets a small,
explicit surface instead of shelling out to `git worktree add`/copying
directories by hand, which would bypass this module's owner-scoping and its
all-or-nothing apply guarantee entirely.

    alt_start    write  start an experiment and add one isolated alternative
                        per label given
    alt_compare  read   diffs of every alternative against the base, plus
                        which files more than one alternative touches
    alt_apply    write  merge one alternative into the main copy

`alt_apply` requires human approval -- the SAME mechanism `git_tools.py`'s
`git_merge` already uses (`ctx["human_approved"]`, sealed by an approval
card the user answered, OR `args["user_confirmed"]` after the model asked
and the user said yes on a retry): no new approval subsystem invented here,
per the CMP-13 contract's own "apply exige aprobación humana como
git_merge". `alt_start`/`alt_compare` are not gated: creating an isolated
sandbox and diffing it touches nothing outside its own isolation and reads
nothing the caller did not already ask for by naming the tool.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

from src import alternatives

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Argument parsing (same permissive JSON-object shape every action-dispatched
# tool in this codebase accepts — see git_tools.py / board_tools.py)
# ---------------------------------------------------------------------------
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


def _project_id(ctx: dict) -> str:
    return str((ctx or {}).get("project_id") or "").strip()


def _no_project(tool: str) -> Dict[str, Any]:
    return {
        "error": f"{tool}: this chat has no project -- an experiment belongs to a project. "
                 "Open or create a project for this chat first.",
        "exit_code": 1, "error_class": "alternatives.no_project",
    }


def _alt_error(tool: str, exc: alternatives.AlternativesError) -> Dict[str, Any]:
    result: Dict[str, Any] = {"error": f"{tool}: {exc}", "exit_code": 1, "error_class": exc.error_class}
    if isinstance(exc, alternatives.ApplyConflictError):
        result["conflicts"] = exc.conflicts
    return result


def _human_approved(ctx: dict, args: Dict[str, Any]) -> bool:
    """Identical check to `git_tools._human_approved` -- see that function's
    own docstring for the full reasoning (sealed approval card, or the
    model's own "asked, user said yes, retried with the flag" record)."""
    return bool((ctx or {}).get("human_approved")) or bool(args.get("user_confirmed"))


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
class AltStartTool:
    """`alt_start`: start a new experiment for the current project's
    workspace and add one isolated alternative per label in `alternatives`
    (default: two, "Alternative 1"/"Alternative 2"). Returns the experiment
    id and each alternative's id/isolation/path -- ALWAYS cite the
    experiment id back to the user, never invent one."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content)
        project_id = _project_id(ctx)
        if not project_id:
            return _no_project("alt_start")
        owner = _owner(ctx)
        goal = str(args.get("goal") or "").strip()
        if not goal:
            return {"error": "alt_start: `goal` is required", "exit_code": 1}

        workspace = str(args.get("workspace") or "").strip()
        if not workspace:
            from src.tool_execution import get_active_workspace
            workspace = get_active_workspace() or ""
        if not workspace:
            return {
                "error": "alt_start: no `workspace` given and no active workspace is bound to this turn -- "
                         "pass `workspace`, or run this inside a chat with a workspace/project set",
                "exit_code": 1, "error_class": "alternatives.invalid_request",
            }

        raw_labels = args.get("alternatives")
        labels: List[str] = [str(x) for x in raw_labels if str(x).strip()] if isinstance(raw_labels, list) and raw_labels else []
        if not labels:
            labels = ["Alternative 1", "Alternative 2"]

        try:
            exp = alternatives.create_experiment(owner, project_id, goal, workspace)
            alts = [alternatives.add_alternative(owner, exp["id"], label) for label in labels]
        except alternatives.AlternativesError as exc:
            return _alt_error("alt_start", exc)

        lines = [f"Experiment {exp['id']} ({exp['base_kind']} @ {exp['base_ref'][:12]})"]
        for a in alts:
            lines.append(f"  {a['id']}: {a['label']} [{a['isolation']}] -> {a['path']}")
        return {
            "output": "\n".join(lines), "exit_code": 0,
            "experiment_id": exp["id"], "base_kind": exp["base_kind"], "base_ref": exp["base_ref"],
            "alternatives": [
                {"id": a["id"], "label": a["label"], "isolation": a["isolation"], "path": a["path"]}
                for a in alts
            ],
        }


class AltCompareTool:
    """`alt_compare`: diffs of every alternative in `experiment_id` against
    the experiment's base, plus which files more than one alternative
    touches (the set `alt_apply`/a combine will need a three-way merge for,
    or that will collide if the same file is taken from two of them).
    Read-only."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content)
        owner = _owner(ctx)
        exp_id = str(args.get("experiment_id") or "").strip()
        if not exp_id:
            return {"error": "alt_compare: `experiment_id` is required", "exit_code": 1}
        try:
            result = alternatives.compare(owner, exp_id)
        except alternatives.AlternativesError as exc:
            return _alt_error("alt_compare", exc)

        lines = [f"base: {result['base_ref'][:12]}"]
        for a in result["alternatives"]:
            summary = a["diff_summary"] or {}
            lines.append(
                f"{a['id']} ({a['label']}, {a['status']}): {summary.get('files_changed', 0)} file(s), "
                f"+{summary.get('additions', 0)}/-{summary.get('deletions', 0)}"
            )
        if result["contested_files"]:
            lines.append("contested (touched by more than one alternative): " + ", ".join(result["contested_files"].keys()))
        return {
            "output": "\n".join(lines), "exit_code": 0,
            "base_ref": result["base_ref"],
            "alternatives": [
                {"id": a["id"], "label": a["label"], "status": a["status"],
                 "diff_summary": a["diff_summary"], "tests_result": a["tests_result"]}
                for a in result["alternatives"]
            ],
            "contested_files": result["contested_files"],
        }


class AltApplyTool:
    """`alt_apply`: merge `alternative_id`'s changes into the main copy of
    `experiment_id`'s workspace. Gated exactly like `git_merge` -- see the
    module docstring. Every touched file is three-way merged (base / the
    main copy now / the alternative); on ANY conflict nothing is written
    and the conflicting paths come back for the model to relay, never a
    guessed resolution."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content)
        if not _human_approved(ctx, args):
            return {
                "error": "alt_apply: refused -- applying an alternative changes the user's main copy. "
                         "Ask the user for explicit approval and retry this EXACT call with "
                         "\"user_confirmed\": true once they say yes.",
                "exit_code": 1, "error_class": "alternatives.approval_required",
            }
        owner = _owner(ctx)
        exp_id = str(args.get("experiment_id") or "").strip()
        alt_id = str(args.get("alternative_id") or "").strip()
        if not exp_id or not alt_id:
            return {"error": "alt_apply: `experiment_id` and `alternative_id` are required", "exit_code": 1}
        try:
            result = alternatives.apply_alternative(owner, exp_id, alt_id)
        except alternatives.AlternativesError as exc:
            return _alt_error("alt_apply", exc)
        if result.get("skipped_same"):
            return {"output": "Nothing to apply: the main copy already matches.", "exit_code": 0, **result}
        return {
            "output": f"Applied {len(result['applied_files'])} file(s) into the main copy.",
            "exit_code": 0, **result,
        }
