"""orphan_workers.py — workers whose parent run is gone.

A delegated worker (``delegate_agents``) runs as a task of this process for
the parent turn that started it. When the parent run ends, is replaced or is
evicted the worker is normally stopped with it; when that does not happen the
worker keeps using the model (and its tools) with nobody waiting for the
result. This module lists those workers, says why each one is considered an
orphan, and stops one on request. It never stops anything by itself.

Parent state (``parent_state``):

* ``running``   the parent's detached run is still going (or the parent is
                itself a live worker): not an orphan;
* ``finished``  the parent run with that exact run id ended (done, stopped,
                error, interrupted): orphan;
* ``replaced``  the parent session now runs a different run: orphan;
* ``gone``      no run of that session is known at all and the worker has been
                running longer than ``GRACE_S``: orphan;
* ``unknown``   the parent has no detached run (a foreground or scheduled
                caller) and the worker is younger than ``GRACE_S``: not judged.

Also reported, as ``dispatch_job``: a worker job whose record says it is live
but that has no running task in this process (its task ended without closing
the record). Stopping one closes the record as ``interrupted``.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

#: A worker younger than this whose parent is not a detached run is not judged.
GRACE_S = 120.0


def _parent_state(parent_session_id: str, parent_run_id: Optional[str], started_at: float,
                  now: float) -> str:
    from src import agent_runs
    from src.agent_tools import subagent_tools
    if not parent_session_id:
        return "unknown"
    live_parent = subagent_tools._WORKER_RUNS.get(parent_session_id)  # noqa: SLF001
    task = subagent_tools._ACTIVE_WORKERS.get(parent_session_id)  # noqa: SLF001
    if live_parent is not None and task is not None and not task.done():
        return "running"
    run = agent_runs._RUNS.get(parent_session_id)  # noqa: SLF001
    if run is not None:
        if parent_run_id and run.run_id != parent_run_id:
            return "replaced"
        return "running" if run.status == "running" else "finished"
    marker = None
    try:
        marker = agent_runs.finished_marker(parent_session_id)
    except Exception:  # noqa: BLE001
        marker = None
    if marker and (not parent_run_id or marker.get("run_id") in (None, parent_run_id)):
        return "finished"
    return "gone" if (now - started_at) >= GRACE_S else "unknown"


def scan(*, owns: Optional[Callable[[str], bool]] = None, now: Optional[float] = None) -> List[Dict[str, Any]]:
    """Orphaned workers of this process. ``owns(session_id)`` restricts the
    list to sessions the caller may see (workers carry no owner of their own:
    the parent session's owner decides)."""
    from src.agent_tools import subagent_tools
    now = time.time() if now is None else now
    out: List[Dict[str, Any]] = []
    for sid, run in list(subagent_tools._WORKER_RUNS.items()):  # noqa: SLF001
        task = subagent_tools._ACTIVE_WORKERS.get(sid)  # noqa: SLF001
        if task is None or task.done():
            continue
        parent = str(getattr(run, "parent_session_id", "") or "")
        started = float(getattr(run, "started", now) or now)
        state = _parent_state(parent, getattr(run, "parent_run_id", None), started, now)
        if state in ("running", "unknown"):
            continue
        if owns is not None and not (owns(parent or sid) or owns(sid)):
            continue
        out.append({
            "kind": "delegated_worker", "session_id": sid, "worker_id": getattr(run, "id", ""),
            "name": getattr(run, "name", ""), "role": getattr(run, "role", ""),
            "parent_session_id": parent, "parent_run_id": getattr(run, "parent_run_id", None),
            "parent_state": state,
            "reason": {"finished": "its parent run already ended", "replaced": "its parent session started another run",
                       "gone": "no run of its parent session is known"}[state],
            "started_at": started, "age_s": round(now - started, 1), "rounds": getattr(run, "rounds", 0),
            "tool_calls": getattr(run, "tool_calls", 0), "last_event_at": getattr(run, "last_event_at", None),
            "idle_s": round(now - float(getattr(run, "last_event_at", started) or started), 1),
            "killable": True, "kill": {"method": "POST", "path": f"/api/agent/orphans/{sid}/kill"},
        })
    out.extend(_stale_dispatch_jobs(owns=owns, now=now))
    out.sort(key=lambda o: o["age_s"], reverse=True)
    return out


def _stale_dispatch_jobs(*, owns: Optional[Callable[[str], bool]], now: float) -> List[Dict[str, Any]]:
    try:
        from src import dispatch
    except Exception:  # noqa: BLE001
        return []
    out = []
    for job in list(dispatch._jobs.values()):  # noqa: SLF001
        if job.status not in dispatch._LIVE:  # noqa: SLF001
            continue
        task = getattr(job, "task", None)
        if task is not None and not task.done():
            continue
        if owns is not None and getattr(job, "session_id", None) and not owns(job.session_id):
            continue
        started = float(job.started or job.created or now)
        out.append({
            "kind": "dispatch_job", "session_id": job.session_id or "", "worker_id": job.id, "name": job.title,
            "role": "", "parent_session_id": job.session_id or "", "parent_run_id": None, "parent_state": "gone",
            "reason": "its record says it is live but no task of this process is running it",
            "started_at": started, "age_s": round(now - started, 1), "rounds": 0, "tool_calls": 0,
            "last_event_at": None, "idle_s": round(now - started, 1), "killable": True,
            "kill": {"method": "POST", "path": f"/api/agent/orphans/{job.id}/kill"},
        })
    return out


def kill(worker_key: str, *, owns: Optional[Callable[[str], bool]] = None,
         reason: str = "orphaned") -> Dict[str, Any]:
    """Stop one orphan found by :func:`scan` (by child session id or dispatch
    job id). Anything that is not currently an orphan is refused: this is not a
    general "stop any worker" switch."""
    found = next((o for o in scan(owns=owns) if worker_key in (o["session_id"], o["worker_id"])), None)
    if found is None:
        return {"stopped": False, "reason": "not an orphan (or not yours)"}
    if found["kind"] == "delegated_worker":
        from src.agent_tools import subagent_tools
        ok = subagent_tools.stop_worker(found["session_id"], reason=reason)
        return {"stopped": bool(ok), "kind": found["kind"], "session_id": found["session_id"],
                "reason": "cancelled" if ok else "it finished while we looked"}
    from src import dispatch
    job = dispatch._jobs.get(found["worker_id"])  # noqa: SLF001
    if job is None:
        return {"stopped": False, "reason": "job not found"}
    job.status = "interrupted"
    job.verdict = job.verdict or "closed as orphaned: no task was running it"
    job.finished = time.time()
    try:
        job._persist()  # noqa: SLF001
    except Exception:  # noqa: BLE001
        logger.debug("dispatch persist after orphan close failed", exc_info=True)
    return {"stopped": True, "kind": found["kind"], "session_id": found["session_id"], "reason": "closed as interrupted"}


__all__ = ["GRACE_S", "kill", "scan"]
