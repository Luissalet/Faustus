"""agent_tools/swarm_tools.py — swarm map: one instruction over N items in
parallel (`src.swarm`).

    swarm_map      start a run (mode llm: one model call per item; mode
                   agent: one limited worker per item). `wait: true` blocks
                   up to `wait_timeout` seconds and returns the table inline
                   when it finishes in time; otherwise the run id.
    swarm_status   progress of a run (counts, parallelism, files).
    swarm_results  the result rows, paged, plus the reduce output.
    swarm_cancel   stop a run; finished items are kept, the rest stay
                   pending (resume with swarm_map `resume_run_id`).

Gating (`src/tool_capabilities.py`): `swarm_map` in llm mode sends the items
to a model and nothing else -- the class of `chat_with_model`; in agent mode
its workers run tools, so it is classed like `delegate_agents` (the workers
keep the caller's own restrictions, see `src.swarm.service._agent_spec`).
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from src.swarm import service as _service
from src.swarm.render import SwarmSpecError
from src.swarm.store import SwarmNotFoundError

logger = logging.getLogger(__name__)

INLINE_ROWS = 60
DEFAULT_WAIT_S = 120.0
MAX_WAIT_S = 900.0

#: The native schemas live in `src.tool_schemas.FUNCTION_TOOL_SCHEMAS` (a
#: literal the registry tests read); these are the names they cover.
TOOL_NAMES = ("swarm_map", "swarm_status", "swarm_results", "swarm_cancel")


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


def _run_id(args: Dict[str, Any]) -> str:
    return str(args.get("run_id") or "").strip()


def _table_text(result: Dict[str, Any], max_rows: int = INLINE_ROWS) -> str:
    from src.swarm import render
    rows = result.get("rows") or []
    cols = result.get("columns") or []
    text = render.to_markdown(rows[:max_rows], cols, cell_limit=200)
    if (result.get("total_rows") or 0) > len(rows[:max_rows]) + int(result.get("offset") or 0):
        text += (f"\n… {result['total_rows']} rows in all; page with swarm_results "
                 f"(offset {int(result.get('offset') or 0) + len(rows[:max_rows])}).\n")
    return text


def _status_line(summary: Dict[str, Any]) -> str:
    c = summary.get("counts") or {}
    par = (summary.get("parallel") or {})
    line = (f"{summary['run_id']}: {summary['status']} — {c.get('ok', 0)} ok, {c.get('failed', 0)} failed, "
            f"{c.get('pending', 0)} pending of {c.get('total', 0)}")
    if par.get("effective"):
        line += f" · {par['effective']} at a time ({par.get('source')})"
    return line


class SwarmMapTool:
    """`swarm_map`: apply one instruction to every item, in parallel."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content)
        owner = _owner(ctx)
        # Progress goes to this tool call's stream only while the call waits
        # for the run; after it returns, the run carries on silently.
        sink: Dict[str, Any] = {"cb": (ctx or {}).get("progress_cb") if args.get("wait") else None}
        resume_id = str(args.get("resume_run_id") or "").strip()

        async def _on_progress(p: Dict[str, Any]) -> None:
            progress_cb = sink["cb"]
            if progress_cb is None:
                return
            await progress_cb({
                "event": "swarm_progress",
                "message": f"{p.get('ok', 0) + p.get('failed', 0)}/{p.get('total', 0)} items "
                           f"({p.get('failed', 0)} failed)",
                "swarm": p,
            })

        try:
            if resume_id:
                _service.resume(resume_id, owner, on_progress=_on_progress)
                run_id = resume_id
            else:
                workspace = str(args.get("workspace") or "").strip()
                roots: List[str] = []
                if str(args.get("mode") or "llm").lower() == "agent":
                    from src.tool_execution import get_active_workspace, get_active_workspace_roots
                    workspace = workspace or (get_active_workspace() or "")
                    roots = list(get_active_workspace_roots() or ())
                run_id = _service.start(
                    owner, str((ctx or {}).get("session_id") or ""),
                    str(args.get("instruction") or ""), args.get("items"),
                    mode=str(args.get("mode") or "llm"),
                    output_fields=args.get("output_fields"), reduce=args.get("reduce"),
                    model=str(args.get("model") or ""), endpoint_id=str(args.get("endpoint_id") or ""),
                    max_items=args.get("max_items"), per_item_timeout=args.get("per_item_timeout"),
                    max_parallel=args.get("max_parallel"), tools=args.get("tools"),
                    max_rounds=args.get("max_rounds"), workspace=workspace,
                    workspace_roots=roots or None, ctx=ctx, on_progress=_on_progress,
                )
        except SwarmSpecError as exc:
            return {"error": f"swarm_map: {exc}", "exit_code": 1, "error_class": "swarm.invalid_request"}
        except SwarmNotFoundError as exc:
            return {"error": str(exc), "exit_code": 1, "error_class": "swarm.not_found"}
        except Exception as exc:  # noqa: BLE001 - report, never crash the turn
            logger.warning("swarm_map failed to start: %s", exc, exc_info=True)
            return {"error": f"swarm_map: {exc}", "exit_code": 1}

        if not args.get("wait"):
            summary = _service.status(run_id, owner)
            return {"output": f"Started {_status_line(summary)}. Poll with swarm_status / swarm_results.",
                    "exit_code": 0, "run_id": run_id, "status": summary}
        try:
            wait_s = float(args.get("wait_timeout") or DEFAULT_WAIT_S)
        except (TypeError, ValueError):
            wait_s = DEFAULT_WAIT_S
        try:
            finished = await _service.wait(run_id, max(1.0, min(MAX_WAIT_S, wait_s)))
        finally:
            sink["cb"] = None
        if not finished:
            summary = _service.status(run_id, owner)
            return {"output": f"Still running after {int(wait_s)} s — {_status_line(summary)}. "
                              "It carries on in the background; poll with swarm_status / swarm_results.",
                    "exit_code": 0, "run_id": run_id, "finished": False, "status": summary}
        result = _service.results(run_id, owner, offset=0, limit=INLINE_ROWS)
        parts = [_status_line(result), "", _table_text(result)]
        reduce_result = result.get("reduce_result") or {}
        if reduce_result.get("output"):
            parts += ["", "Reduce:", reduce_result["output"]]
        elif reduce_result.get("error"):
            parts += ["", f"Reduce failed: {reduce_result['error']}"]
        if result.get("files"):
            parts += ["", "Files: " + ", ".join(result["files"])]
        return {"output": "\n".join(parts), "exit_code": 0, "run_id": run_id, "finished": True,
                "status": {k: result.get(k) for k in ("status", "counts", "parallel", "files", "artifacts")},
                "rows": result.get("rows"), "reduce_result": reduce_result or None}


class SwarmStatusTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content)
        run_id = _run_id(args)
        owner = _owner(ctx)
        try:
            if not run_id:
                runs = _service.list_runs(owner, limit=10)
                lines = [_status_line(r) for r in runs] or ["No swarm runs yet."]
                return {"output": "\n".join(lines), "exit_code": 0, "runs": runs}
            summary = _service.status(run_id, owner)
        except SwarmNotFoundError as exc:
            return {"error": str(exc), "exit_code": 1, "error_class": "swarm.not_found"}
        line = _status_line(summary)
        if summary.get("resumable"):
            line += " — resumable: call swarm_map with resume_run_id"
        return {"output": line, "exit_code": 0, **summary}


class SwarmResultsTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content)
        run_id = _run_id(args)
        if not run_id:
            return {"error": "swarm_results: `run_id` is required", "exit_code": 1}
        try:
            result = _service.results(run_id, _owner(ctx), offset=int(args.get("offset") or 0),
                                      limit=int(args.get("limit") or 50),
                                      status_filter=str(args.get("status") or ""))
        except SwarmNotFoundError as exc:
            return {"error": str(exc), "exit_code": 1, "error_class": "swarm.not_found"}
        except (TypeError, ValueError) as exc:
            return {"error": f"swarm_results: {exc}", "exit_code": 1}
        text = _status_line(result) + "\n\n" + _table_text(result, max_rows=result["limit"])
        reduce_result = result.get("reduce_result") or {}
        if reduce_result.get("output") and not int(args.get("offset") or 0):
            text += "\nReduce:\n" + reduce_result["output"]
        return {"output": text, "exit_code": 0, **result}


class SwarmCancelTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content)
        run_id = _run_id(args)
        if not run_id:
            return {"error": "swarm_cancel: `run_id` is required", "exit_code": 1}
        try:
            result = _service.cancel(run_id, _owner(ctx))
        except SwarmNotFoundError as exc:
            return {"error": str(exc), "exit_code": 1, "error_class": "swarm.not_found"}
        msg = (f"Cancelled {run_id}; finished items are kept, the rest stay pending."
               if result.get("cancelled") else f"{run_id} had already ended ({result.get('status')}).")
        return {"output": msg, "exit_code": 0, **result}
