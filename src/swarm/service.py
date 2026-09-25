"""src/swarm/service.py — the front door for the agent tools
(`src.agent_tools.swarm_tools`) and the HTTP routes (`routes.swarm_routes`):
`start`, `wait`, `status`, `results`, `cancel`, `resume`, `list_runs`.

Everything durable is `src.swarm.store`'s checkpoint; `_TASKS` only says
whether THIS process is running a run right now (a run whose manifest says
"running" with no task here was interrupted and is resumable).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from typing import Any, Dict, List, Optional

from src.swarm import capacity, render, runner, store
from src.swarm.render import SwarmSpecError
from src.swarm.store import SwarmNotFoundError

logger = logging.getLogger(__name__)

_TASKS: Dict[str, asyncio.Task] = {}
MODES = ("llm", "agent")
LIVE = ("queued", "running")

__all__ = ["start", "schedule", "wait", "status", "results", "cancel", "resume", "list_runs",
           "export_path", "SwarmSpecError", "SwarmNotFoundError"]


def _agent_spec(ctx: Optional[Dict[str, Any]], tools: Optional[List[str]], max_rounds: Optional[int],
                workspace: str, roots: List[str]) -> Dict[str, Any]:
    """Derive the per-item worker's permissions NOW, from the caller's own
    standing: a restriction on the caller flows down, a worker cannot start
    more workers, and a caller that is itself a worker at the depth ceiling
    is refused (`src.subagent_permissions`)."""
    from src.agent_defs import AgentDef, known_tools
    from src.agent_tools.subagent_tools import _parent_standing
    from src.subagent_permissions import derive
    vocabulary = known_tools()
    wanted = tuple(sorted({str(t) for t in (tools or []) if str(t).strip()}))
    if vocabulary:
        unknown = [t for t in wanted if t not in vocabulary]
        if unknown:
            raise SwarmSpecError(f"unknown tool(s) for the swarm workers: {', '.join(unknown)}")
    wanted = tuple(t for t in wanted if t not in runner.RECURSION_TOOLS)
    definition = AgentDef(slug="swarm-worker", name="swarm worker", mode="worker",
                          tools=wanted, deny=tuple(sorted(runner.RECURSION_TOOLS)))
    parent = _parent_standing(dict(ctx or {}), workspace or None, roots)
    from src.subagent_permissions import DepthExceeded
    try:
        perms = derive(parent, definition, parent_depth=int(getattr(parent, "depth", 0)),
                       workspace_roots=roots, workspace=workspace, vocabulary=vocabulary)
    except DepthExceeded as exc:
        raise SwarmSpecError(f"swarm_map (agent mode): {exc}") from exc
    rounds = int(max_rounds or runner.DEFAULT_AGENT_ROUNDS)
    return {"permissions": perms.to_dict(), "workspace": workspace, "workspace_roots": roots,
            "max_rounds": max(1, min(runner.MAX_AGENT_ROUNDS, rounds)), "tools": list(wanted)}


def start(owner: str, session_id: str, instruction: str, items: Any, *, mode: str = "llm",
          output_fields: Any = None, reduce: Optional[str] = None, model: str = "",
          endpoint_id: str = "", max_items: Optional[int] = None,
          per_item_timeout: Optional[float] = None, max_parallel: Optional[int] = None,
          tools: Optional[List[str]] = None, max_rounds: Optional[int] = None,
          workspace: str = "", workspace_roots: Optional[List[str]] = None,
          ctx: Optional[Dict[str, Any]] = None, run_in_background: bool = True,
          on_progress: runner.ProgressCb = None) -> str:
    """Validate, write the checkpoint and (by default) schedule the run on the
    current loop. Returns the run id at once; poll `status`/`results`."""
    instruction = str(instruction or "").strip()
    if not instruction:
        raise SwarmSpecError("`instruction` is required")
    mode = str(mode or "llm").strip().lower()
    if mode not in MODES:
        raise SwarmSpecError(f"`mode` must be one of {', '.join(MODES)}")
    limit = capacity.max_items()
    if max_items:
        limit = max(1, min(limit, int(max_items)))
    clean_items = render.normalize_items(items, limit)
    fields = render.normalize_fields(output_fields)
    timeout = runner.DEFAULT_AGENT_TIMEOUT if mode == "agent" else runner.DEFAULT_LLM_TIMEOUT
    if per_item_timeout:
        timeout = max(5.0, min(runner.MAX_ITEM_TIMEOUT, float(per_item_timeout)))

    run_id = f"swarm-{uuid.uuid4().hex[:12]}"
    manifest: Dict[str, Any] = {
        "run_id": run_id, "owner": owner or "", "session_id": session_id or "",
        "created_at": time.time(), "started_at": None, "finished_at": None,
        "status": "queued", "mode": mode, "instruction": instruction,
        "output_fields": fields, "reduce": (str(reduce).strip() or None) if reduce else None,
        "route": {"endpoint_id": endpoint_id or "", "model": model or "", "session_id": session_id or ""},
        "per_item_timeout": timeout,
        "max_parallel": int(max_parallel) if max_parallel else 0,
        "counts": {"total": len(clean_items), "ok": 0, "failed": 0, "pending": len(clean_items)},
        "artifacts": [], "files": [], "error": None,
    }
    if mode == "agent":
        roots = [str(r) for r in (workspace_roots or ([workspace] if workspace else [])) if r]
        manifest["agent"] = _agent_spec(ctx, tools, max_rounds, workspace or "", roots)
    os.makedirs(store.run_dir(run_id), exist_ok=True)
    store.save_items(run_id, clean_items)
    store.save_manifest(manifest)
    if run_in_background:
        schedule(run_id, owner, on_progress=on_progress)
    return run_id


def schedule(run_id: str, owner: str, *, on_progress: runner.ProgressCb = None) -> asyncio.Task:
    existing = _TASKS.get(run_id)
    if existing is not None and not existing.done():
        return existing
    task = asyncio.ensure_future(runner.run(run_id, owner, on_progress=on_progress))
    _TASKS[run_id] = task

    def _done(t: asyncio.Task) -> None:
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            logger.warning("swarm %s: run crashed: %s", run_id, exc)
    task.add_done_callback(_done)
    return task


def resume(run_id: str, owner: str, *, on_progress: runner.ProgressCb = None) -> Dict[str, Any]:
    manifest = store.manifest_for(run_id, owner)
    task = _TASKS.get(run_id)
    if task is not None and not task.done():
        return {"run_id": run_id, "resumed": False, "detail": "already running"}
    pending = manifest.get("counts", {}).get("pending")
    schedule(run_id, owner, on_progress=on_progress)
    return {"run_id": run_id, "resumed": True, "pending": pending}


async def wait(run_id: str, timeout: float) -> bool:
    """True when the run finished within `timeout` seconds. Never cancels it."""
    task = _TASKS.get(run_id)
    if task is None:
        return False
    done, _ = await asyncio.wait({task}, timeout=max(0.0, timeout))
    return bool(done)


def _effective_status(manifest: Dict[str, Any]) -> str:
    status = str(manifest.get("status") or "")
    if status in LIVE:
        task = _TASKS.get(manifest["run_id"])
        if task is None or task.done():
            return "interrupted"
    return status


def _summary(manifest: Dict[str, Any]) -> Dict[str, Any]:
    counts = dict(manifest.get("counts") or {})
    total = int(counts.get("total") or 0)
    finished = int(counts.get("ok") or 0) + int(counts.get("failed") or 0)
    status = _effective_status(manifest)
    return {
        "run_id": manifest["run_id"], "status": status, "mode": manifest.get("mode"),
        "instruction": manifest.get("instruction"), "session_id": manifest.get("session_id"),
        "created_at": manifest.get("created_at"), "started_at": manifest.get("started_at"),
        "finished_at": manifest.get("finished_at"), "counts": counts,
        "progress": round(finished / total, 4) if total else 0.0,
        "parallel": manifest.get("parallel"), "resolved": manifest.get("resolved"),
        "output_fields": [f["name"] for f in manifest.get("output_fields") or []],
        "reduce": manifest.get("reduce"),
        "reduce_status": (manifest.get("reduce_result") or {}).get("status"),
        "files": list(manifest.get("files") or []),
        "artifacts": list(manifest.get("artifacts") or []),
        "error": manifest.get("error"),
        "resumable": status in ("interrupted", "cancelled") and int(counts.get("pending") or 0) > 0,
    }


def status(run_id: str, owner: str) -> Dict[str, Any]:
    return _summary(store.manifest_for(run_id, owner))


def list_runs(owner: str, limit: int = 50) -> List[Dict[str, Any]]:
    return [_summary(m) for m in store.list_manifests(owner)[: max(1, int(limit))]]


def results(run_id: str, owner: str, *, offset: int = 0, limit: int = 50,
            status_filter: str = "") -> Dict[str, Any]:
    manifest = store.manifest_for(run_id, owner)
    items = store.load_items(run_id)
    rows = store.load_results(run_id)
    fields = manifest.get("output_fields") or None
    records = render.table_rows(list(rows.values()), items, fields)
    if status_filter:
        records = [r for r in records if r.get("status") == status_filter]
    offset = max(0, int(offset or 0))
    limit = max(1, min(500, int(limit or 50)))
    page = records[offset: offset + limit]
    return {
        **_summary(manifest), "offset": offset, "limit": limit, "total_rows": len(records),
        "next_offset": offset + limit if offset + limit < len(records) else None,
        "columns": render.columns(fields), "rows": page,
        "reduce_result": manifest.get("reduce_result"),
    }


def cancel(run_id: str, owner: str) -> Dict[str, Any]:
    manifest = store.manifest_for(run_id, owner)
    status_now = _effective_status(manifest)
    if status_now in runner.TERMINAL:
        return {"run_id": run_id, "cancelled": False, "status": status_now}
    manifest["cancel_requested"] = True
    task = _TASKS.get(run_id)
    if task is None or task.done():
        # Nothing runs it in this process: mark it, keep the finished items.
        manifest["status"] = "cancelled"
        manifest["finished_at"] = time.time()
    store.save_manifest(manifest)
    in_flight = runner.request_cancel(run_id)
    return {"run_id": run_id, "cancelled": True, "in_flight_cancelled": in_flight}


def export_path(run_id: str, owner: str, name: str) -> str:
    manifest = store.manifest_for(run_id, owner)
    if name not in (manifest.get("files") or []):
        raise SwarmNotFoundError(f"no file {name!r} in swarm run {run_id!r}")
    path = os.path.join(store.exports_dir(run_id), name)
    if not os.path.isfile(path):
        raise SwarmNotFoundError(f"no file {name!r} in swarm run {run_id!r}")
    return path
