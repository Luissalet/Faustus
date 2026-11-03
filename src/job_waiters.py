"""job_waiters.py — how a recipe execution waits for tools that start a job.

``recipe_execution`` counts a step as finished only when it finished. Processes
(``process_start``) and ``bash`` background jobs are waited on through
``process_read``; this registry covers the other tools that return a job id AND
have an existing status/read tool or function:

    tool               job id                   how it is polled
    generate_image     request_id (pending)     image_job {request_id, action: status}
    image_job          request_id (pending)     image_job {request_id, action: status}
    fanout_run         run_id                   fanout_status {run_id}
    research_podcast   session_id (running)     research_podcast {research_id, action: status}
    trigger_research   research_session_id      the research record on disk (no status tool exists)

Each entry says how to recognise "this call started a job that has not
finished" in the start result (``detect``), how to ask about it (``poll``: a
tool call through the same executor, so the caller's policy applies, or a plain
function) and what the answer means (``judge`` -> running / done / failed).
A tool that is not here keeps being reported as ``queued``: nothing this module
cannot poll is ever counted as finished.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

Executor = Callable[[str, Dict[str, Any], str], Any]

RUNNING, DONE, FAILED = "running", "done", "failed"
POLL_INTERVAL = 2.0


@dataclass(frozen=True)
class JobWaiter:
    tool: str
    id_key: str                                              # name under which the job id is reported
    detect: Callable[[Mapping[str, Any]], Optional[str]]     # start result -> job id, or None
    poll: Callable[[Executor, str, str, str], Any]           # (executor, job_id, call_id, owner) -> raw status
    judge: Callable[[Any], Tuple[str, str]]                  # raw status -> (RUNNING|DONE|FAILED, detail)


def _str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _as_mapping(raw: Any) -> Mapping[str, Any]:
    if isinstance(raw, Mapping):
        return raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
            return data if isinstance(data, Mapping) else {}
        except ValueError:
            return {}
    return {}


# -- images (the Prospero-backed request receipt) --------------------------------------------

def _detect_image(result: Mapping[str, Any]) -> Optional[str]:
    if str(result.get("state") or "").lower() == "pending":
        return _str(result.get("request_id")) or None
    return None


def _poll_image(executor: Executor, job_id: str, call_id: str, owner: str) -> Any:
    return executor("image_job", {"request_id": job_id, "action": "status"}, call_id)


def _judge_image(raw: Any) -> Tuple[str, str]:
    r = _as_mapping(raw)
    state = str(r.get("state") or "").lower()
    if r.get("image_url") or r.get("image_id"):
        return DONE, "image delivered"
    if state == "pending":
        return RUNNING, "image still pending"
    if state in ("failed", "cancelled"):
        return FAILED, f"image job {state}"
    if state == "unknown" or r.get("status") == "outcome_unknown":
        return FAILED, "image outcome unknown (not repeated, check it with image_job)"
    if r.get("error"):
        return FAILED, str(r["error"])[:300]
    return RUNNING, "no final image yet"


# -- fan-out runs -------------------------------------------------------------------------------

def _detect_fanout(result: Mapping[str, Any]) -> Optional[str]:
    if result.get("error") or (isinstance(result.get("exit_code"), int) and result["exit_code"] != 0):
        return None
    return _str(result.get("run_id")) or None


def _poll_fanout(executor: Executor, job_id: str, call_id: str, owner: str) -> Any:
    return executor("fanout_status", {"run_id": job_id}, call_id)


def _judge_fanout(raw: Any) -> Tuple[str, str]:
    r = _as_mapping(raw)
    if r.get("error"):
        return FAILED, str(r["error"])[:300]
    status = str(r.get("status") or "").lower()
    cands = [c for c in (r.get("candidates") or []) if isinstance(c, Mapping)]
    if status == "done":
        if cands and all(str(c.get("state")) == "error" for c in cands):
            return FAILED, "every candidate ended in error"
        return DONE, f"{len(cands)} candidate(s) finished"
    if status in ("error", "failed", "cancelled"):
        return FAILED, f"fan-out {status}"
    if r.get("task_alive") is False and status in ("queued", "running"):
        return FAILED, "no task is running this fan-out any more (the server restarted?)"
    return RUNNING, f"fan-out {status or 'running'}"


# -- research podcasts --------------------------------------------------------------------------

def _detect_podcast(result: Mapping[str, Any]) -> Optional[str]:
    if str(result.get("status") or "").lower() == "running":
        return _str(result.get("session_id")) or None
    return None


def _poll_podcast(executor: Executor, job_id: str, call_id: str, owner: str) -> Any:
    return executor("research_podcast", {"research_id": job_id, "action": "status"}, call_id)


def _judge_podcast(raw: Any) -> Tuple[str, str]:
    r = _as_mapping(raw)
    if r.get("error") and not r.get("status"):
        return FAILED, str(r["error"])[:300]
    status = str(r.get("status") or "").lower()
    if status == "done":
        return DONE, "podcast ready"
    if status == "failed":
        return FAILED, f"podcast failed: {str(r.get('error') or 'unknown error')[:200]}"
    if status == "none":
        return FAILED, "the podcast job disappeared"
    return RUNNING, "podcast running"


# -- deep research (no status tool: the research record is the status) --------------------------

def _detect_research(result: Mapping[str, Any]) -> Optional[str]:
    if result.get("error") or (isinstance(result.get("exit_code"), int) and result["exit_code"] != 0):
        return None
    return _str(result.get("research_session_id") or result.get("session_id")) or None


def _poll_research(executor: Executor, job_id: str, call_id: str, owner: str) -> Any:
    import pathlib
    from src import research_handler as rh
    path = rh._research_json_path(job_id)
    if path is None or not pathlib.Path(path).exists():
        return {"status": "running", "note": "no record yet"}
    try:
        data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"status": "running", "note": "record being written"}
    if isinstance(data, dict) and data.get("owner") not in (None, "", owner):
        return {"status": "failed", "error": "the research belongs to another account"}
    out = {"status": data.get("status", "done") if isinstance(data, dict) else "done"}
    if isinstance(data, dict) and data.get("error"):
        out["error"] = str(data["error"])
    return out


def _judge_research(raw: Any) -> Tuple[str, str]:
    r = _as_mapping(raw)
    status = str(r.get("status") or "").lower()
    if status in ("done", "completed"):
        return DONE, "research report saved"
    if status in ("error", "failed", "cancelled", "interrupted"):
        return FAILED, f"research {status}" + (f": {str(r['error'])[:200]}" if r.get("error") else "")
    return RUNNING, "research running"


WAITERS: Dict[str, JobWaiter] = {w.tool: w for w in (
    JobWaiter("generate_image", "request_id", _detect_image, _poll_image, _judge_image),
    JobWaiter("image_job", "request_id", _detect_image, _poll_image, _judge_image),
    JobWaiter("fanout_run", "run_id", _detect_fanout, _poll_fanout, _judge_fanout),
    JobWaiter("research_podcast", "session_id", _detect_podcast, _poll_podcast, _judge_podcast),
    JobWaiter("trigger_research", "research_session_id", _detect_research, _poll_research, _judge_research),
)}


def find(tool: str, result: Any) -> Optional[Tuple[JobWaiter, str]]:
    """``(waiter, job id)`` when ``tool``'s start result names a job that has
    not finished and this registry can poll it; otherwise None."""
    waiter = WAITERS.get(tool)
    if waiter is None or not isinstance(result, Mapping):
        return None
    job_id = waiter.detect(result)
    return (waiter, job_id) if job_id else None


def wait(waiter: JobWaiter, job_id: str, executor: Executor, call_id: str, owner: str, timeout: float, *,
         interval: Optional[float] = None, sleep: Callable[[float], None] = time.sleep,
         clock: Callable[[], float] = time.monotonic) -> Tuple[str, str, Any]:
    """Poll until the job is done or failed or ``timeout`` seconds pass.
    Returns ``(state, detail, last raw status)`` with state ``done`` /
    ``failed`` / ``timeout``. A poll that raises is reported as failed with the
    exception, never swallowed into "finished"."""
    step = POLL_INTERVAL if interval is None else interval
    deadline = clock() + max(0.0, timeout)
    n = 0
    last: Any = None
    while True:
        n += 1
        try:
            last = waiter.poll(executor, job_id, f"{call_id}:wait{n}", owner)
        except Exception as exc:  # noqa: BLE001 - the step reports it
            return FAILED, f"polling {waiter.tool} {job_id}: {type(exc).__name__}: {exc}", last
        state, detail = waiter.judge(last)
        if state == DONE:
            return DONE, detail, last
        if state == FAILED:
            return FAILED, detail, last
        if clock() >= deadline:
            return "timeout", f"{waiter.tool} {job_id}: {detail} after {int(timeout)}s", last
        sleep(max(0.0, min(step, deadline - clock())))
