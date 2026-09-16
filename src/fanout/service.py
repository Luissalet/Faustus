"""src/fanout/service.py — the front door for both the agent tools
(`src.agent_tools.fanout_tools`) and the HTTP routes (`routes.fanout_routes`):
`start`, `status`, `results`, `apply`. Persistence is entirely
`src.fanout.runner`'s checkpoint files under `DATA_DIR/fanout/`; this module
adds nothing of its own to disk.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Dict, List, Optional

from src.fanout import merge as _merge
from src.fanout import runner as _runner
from src.fanout import score as _score
from src.fanout.plan import FanoutPlan

logger = logging.getLogger(__name__)

#: run_id -> background asyncio.Task, for this process only. A run whose
#: task is not (or no longer) in here is exactly as resumable as one that
#: is -- `runner.run_all` decides purely from each candidate's checkpoint
#: state, never from whether a task object still exists in memory (rule:
#: this dict is a convenience for `status()`'s `task_alive` flag, not the
#: source of truth).
_TASKS: Dict[str, asyncio.Task] = {}


def start(owner: str, plan: FanoutPlan, *, coordinator_model: str = "",
          coordinator_endpoint_url: str = "", coordinator_headers: Optional[Dict[str, str]] = None,
          run_in_background: bool = True,
          on_update: Optional[Callable[[Dict[str, Any]], Any]] = None) -> str:
    """Set up the run (one isolated alternative per candidate) and, unless
    `run_in_background=False` (tests / callers that want to drive
    `resume`/`run_all` themselves), schedule it on the current event loop.
    Returns the run id immediately either way -- callers poll `status`."""
    run_id = _runner.start(owner, plan)
    if run_in_background:
        schedule(run_id, owner, coordinator_model=coordinator_model,
                 coordinator_endpoint_url=coordinator_endpoint_url,
                 coordinator_headers=coordinator_headers, on_update=on_update)
    return run_id


def schedule(run_id: str, owner: str, *, coordinator_model: str = "",
             coordinator_endpoint_url: str = "", coordinator_headers: Optional[Dict[str, str]] = None,
             on_update: Optional[Callable[[Dict[str, Any]], Any]] = None) -> asyncio.Task:
    """Run (or resume) `run_id` as a background task on the current loop.
    Safe to call again for a run already in flight in THIS process (the
    prior task is left alone); for a run whose process died mid-way, this
    is the resume path -- `runner.run_all` only touches non-terminal
    candidates."""
    existing = _TASKS.get(run_id)
    if existing is not None and not existing.done():
        return existing
    task = asyncio.ensure_future(_runner.run_all(
        run_id, owner, coordinator_endpoint_url=coordinator_endpoint_url,
        coordinator_model=coordinator_model, coordinator_headers=coordinator_headers,
        on_update=on_update,
    ))
    _TASKS[run_id] = task
    return task


def resume(run_id: str, owner: str, **kwargs: Any) -> asyncio.Task:
    """Explicit alias for `schedule`, named the way the contract names it:
    picks up every candidate this run left unfinished."""
    return schedule(run_id, owner, **kwargs)


def status(run_id: str, owner: str) -> Dict[str, Any]:
    manifest = _runner._manifest_or_error(run_id, owner)  # noqa: SLF001 - same package
    candidates = _runner.load_all_candidates(run_id)
    task = _TASKS.get(run_id)
    return {
        "run_id": run_id, "status": manifest.get("status"),
        "created_at": manifest.get("created_at"), "started_at": manifest.get("started_at"),
        "finished_at": manifest.get("finished_at"),
        "exp_id": manifest.get("exp_id"),
        "task_alive": bool(task is not None and not task.done()),
        "candidates": [
            {"label": c.get("label"), "state": c.get("state"), "model": c.get("model"),
             "error": c.get("error"), "started_at": c.get("started_at"),
             "finished_at": c.get("finished_at")}
            for c in candidates
        ],
    }


def results(run_id: str, owner: str, *, judge: Optional[Callable[[Dict[str, Any]], float]] = None) -> Dict[str, Any]:
    manifest = _runner._manifest_or_error(run_id, owner)  # noqa: SLF001 - same package
    candidates = _runner.load_all_candidates(run_id)
    ranking = _score.rank(candidates, judge=judge)
    by_label = {c["label"]: c for c in ranking}
    diffs: Dict[str, Any] = {}
    for c in candidates:
        diffs[c.get("label")] = c.get("diff")
    return {
        "run_id": run_id, "status": manifest.get("status"), "exp_id": manifest.get("exp_id"),
        "ranking": ranking,
        "winner": ranking[0]["label"] if ranking else None,
        "diffs": diffs,
        "candidates": candidates,
    }


def apply(run_id: str, owner: str, label: str, *, confirm: bool = False) -> Dict[str, Any]:
    return _merge.apply_winner(owner, run_id, label, confirm=confirm)


def diff_between(run_id: str, owner: str, label_a: str, label_b: str) -> Dict[str, Any]:
    return _merge.diff_between(owner, run_id, label_a, label_b)
