"""agent_tools/fanout_tools.py — R3 (Reach wave): fan one prompt across N
candidate models/endpoints, each racing inside its own isolated
`src.alternatives` alternative, then rank them with `src.fanout.score` and
merge the winner with `src.fanout.merge` (`alt_apply`'s own two-step
propose/confirm gate, reused rather than a second approval mechanism).

    fanout_run      write  start a run: N isolated candidates, same prompt
    fanout_status   read   per-candidate state (queued/running/done/error)
    fanout_results  read   ranked scoreboard + diffs, once candidates finish
    fanout_apply    write  merge one candidate's changes into the main copy

Gating follows `alternatives_tools.py` exactly: `fanout_run`/`fanout_status`/
`fanout_results` touch nothing outside each candidate's own isolation and
read only what the caller asked for by naming the tool; `fanout_apply`
requires the same human-approval record `git_merge`/`alt_apply` already
use.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

from src.fanout import runner as _runner
from src.fanout import service as _service
from src.fanout.plan import FanoutCandidate, FanoutPlan, from_settings_default_candidates

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


def _project_id(ctx: dict) -> str:
    return str((ctx or {}).get("project_id") or "").strip()


def _human_approved(ctx: dict, args: Dict[str, Any]) -> bool:
    return bool((ctx or {}).get("human_approved")) or bool(args.get("user_confirmed") or args.get("confirm"))


def _coordinator_route(ctx: dict) -> Dict[str, Any]:
    """Best-effort (endpoint_url, model, headers) for the chat this tool
    call is running inside -- exactly what `DelegateAgentsTool` reads off
    the parent session, reused here as the default route for a candidate
    that names neither `endpoint_url` nor `endpoint_id`."""
    out = {"endpoint_url": "", "model": "", "headers": None}
    session_id = (ctx or {}).get("session_id")
    if not session_id:
        return out
    try:
        from src.ai_interaction import get_session_manager
        sm = get_session_manager()
        parent = sm.get_session(session_id) if sm else None
    except Exception:
        parent = None
    if parent is None:
        return out
    out["endpoint_url"] = str(getattr(parent, "endpoint_url", "") or "")
    out["model"] = str(getattr(parent, "model", "") or "")
    out["headers"] = getattr(parent, "headers", None) or None
    return out


class FanoutRunTool:
    """`fanout_run`: race the SAME prompt across N candidates, each isolated
    (`src.alternatives`). Returns immediately with `run_id` -- poll with
    `fanout_status`/`fanout_results`, do not block waiting for it here."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content)
        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            return {"error": "fanout_run: `prompt` is required", "exit_code": 1}
        owner = _owner(ctx)
        project_id = _project_id(ctx)

        workspace = str(args.get("workspace") or "").strip()
        if not workspace:
            from src.tool_execution import get_active_workspace
            workspace = get_active_workspace() or ""
        if not workspace:
            return {
                "error": "fanout_run: no `workspace` given and no active workspace is bound to this "
                         "turn -- pass `workspace`, or run this inside a chat with a workspace/project set",
                "exit_code": 1, "error_class": "fanout.invalid_request",
            }

        route = _coordinator_route(ctx)
        raw_candidates = args.get("candidates")
        if isinstance(raw_candidates, list) and raw_candidates:
            candidates = [FanoutCandidate.from_dict(c) for c in raw_candidates if isinstance(c, dict)]
        else:
            candidates = from_settings_default_candidates(
                coordinator_model=route["model"], coordinator_endpoint_url=route["endpoint_url"],
            )
        if not candidates:
            return {"error": "fanout_run: no candidates configured and none could be derived from settings",
                    "exit_code": 1, "error_class": "fanout.invalid_request"}

        max_rounds = int(args.get("max_rounds") or 8)
        budget_tokens = int(args.get("budget_tokens") or 0)
        plan = FanoutPlan(
            prompt=prompt, workspace=workspace, candidates=candidates,
            max_rounds=max_rounds, budget_tokens=budget_tokens,
            project_id=project_id, goal=str(args.get("goal") or prompt),
        )
        try:
            run_id = _service.start(
                owner, plan, coordinator_model=route["model"],
                coordinator_endpoint_url=route["endpoint_url"], coordinator_headers=route["headers"],
            )
        except (ValueError, Exception) as exc:  # noqa: BLE001 - report, never crash the turn
            return {"error": f"fanout_run: {exc}", "exit_code": 1}
        return {
            "output": f"Started fan-out {run_id} with {len(candidates)} candidate(s): "
                      + ", ".join(c.label for c in candidates),
            "exit_code": 0, "run_id": run_id,
            "candidates": [c.label for c in candidates],
        }


class FanoutStatusTool:
    """`fanout_status`: per-candidate state for a fan-out run. Read-only."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content)
        run_id = str(args.get("run_id") or "").strip()
        if not run_id:
            return {"error": "fanout_status: `run_id` is required", "exit_code": 1}
        owner = _owner(ctx)
        try:
            result = _service.status(run_id, owner)
        except _runner.FanoutNotFoundError as exc:
            return {"error": str(exc), "exit_code": 1, "error_class": "fanout.not_found"}
        lines = [f"{run_id}: {result['status']}"]
        for c in result["candidates"]:
            lines.append(f"  {c['label']}: {c['state']}" + (f" ({c['error']})" if c.get("error") else ""))
        return {"output": "\n".join(lines), "exit_code": 0, **result}


class FanoutResultsTool:
    """`fanout_results`: ranked scoreboard (tests/harness/diff size/cost/
    latency, weighted) plus each candidate's diff against the base.
    Read-only."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content)
        run_id = str(args.get("run_id") or "").strip()
        if not run_id:
            return {"error": "fanout_results: `run_id` is required", "exit_code": 1}
        owner = _owner(ctx)
        try:
            result = _service.results(run_id, owner)
        except _runner.FanoutNotFoundError as exc:
            return {"error": str(exc), "exit_code": 1, "error_class": "fanout.not_found"}
        lines = [f"{run_id}: winner so far -> {result['winner']}"]
        for row in result["ranking"]:
            lines.append(f"  #{row['rank']} {row['label']}: {row['total_score']} -- {row['reasoning']}")
        return {"output": "\n".join(lines), "exit_code": 0, **result}


class FanoutApplyTool:
    """`fanout_apply`: merge one candidate's changes into the user's main
    copy. Same gate as `alt_apply` -- refused unless the user explicitly
    approved this exact call."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content)
        run_id = str(args.get("run_id") or "").strip()
        label = str(args.get("label") or "").strip()
        if not run_id or not label:
            return {"error": "fanout_apply: `run_id` and `label` are required", "exit_code": 1}
        owner = _owner(ctx)
        confirm = _human_approved(ctx, args)
        try:
            result = _service.apply(run_id, owner, label, confirm=confirm)
        except _runner.FanoutNotFoundError as exc:
            return {"error": str(exc), "exit_code": 1, "error_class": "fanout.not_found"}
        except Exception as exc:  # noqa: BLE001 - alternatives.ApplyConflictError included
            from src import alternatives as _alternatives
            if isinstance(exc, _alternatives.ApplyConflictError):
                return {"error": str(exc), "exit_code": 1, "error_class": exc.error_class,
                        "conflicts": exc.conflicts}
            return {"error": f"fanout_apply: {exc}", "exit_code": 1}
        if not confirm:
            return {
                "output": "Proposed apply (nothing written yet) -- ask the user for explicit approval "
                          "and retry this EXACT call with \"user_confirmed\": true once they say yes.",
                "exit_code": 0, **result,
            }
        return {"output": f"Applied {label} from fan-out {run_id} into the main copy.",
                "exit_code": 0, **result}
