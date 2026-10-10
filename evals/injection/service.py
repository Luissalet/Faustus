"""Async job service for the injection battery (used by the REST route and the
harness MCP server). Deterministic runs need no model; live runs are opt-in and
need an explicit model + endpoint. Reports persist under
DATA_DIR/injection_evals/ so a restart does not lose them."""
from __future__ import annotations

import glob
import json
import os
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from . import runner

_JOBS: Dict[str, Dict[str, Any]] = {}
_LOCK = threading.Lock()
_MAX_JOBS = 50


def _reports_dir() -> str:
    from src.constants import DATA_DIR
    return os.path.join(DATA_DIR, "injection_evals")


def _public(job: Dict[str, Any]) -> Dict[str, Any]:
    out = {k: v for k, v in job.items() if k != "summary"}
    s = job.get("summary")
    if s:
        out["totals"] = {k: s[k] for k in ("cases", "held", "violated", "errors", "known_holes_open",
                                            "violated_ids", "controls_ok")}
    return out


def validate(params: Dict[str, Any]) -> Dict[str, Any]:
    """Normalise request params; raise ValueError for a bad request."""
    live = bool(params.get("live"))
    case_ids = params.get("case_ids") or None
    if case_ids is not None and not (isinstance(case_ids, list) and all(isinstance(c, str) for c in case_ids)):
        raise ValueError("case_ids must be a list of strings")
    if case_ids:
        try:
            runner.select_cases(case_ids)
        except KeyError as exc:
            raise ValueError(str(exc.args[0]))
    clean: Dict[str, Any] = {"live": live, "case_ids": case_ids}
    if live:
        for k in ("base_url", "endpoint_url", "model"):
            if not str(params.get(k) or "").strip():
                raise ValueError(f"live mode needs '{k}'")
            clean[k] = str(params[k]).strip()
        clean["timeout"] = float(params.get("timeout") or 180.0)
    return clean


def _execute(job_id: str, p: Dict[str, Any]) -> None:
    job = _JOBS[job_id]
    try:
        if p["live"]:
            summary = runner.run_live(base_url=p["base_url"], endpoint_url=p["endpoint_url"],
                                      model=p["model"], case_ids=p["case_ids"], timeout=p["timeout"])
        else:
            summary = runner.run_deterministic(p["case_ids"])
        try:
            job["files"] = runner.persist(summary, _reports_dir())
        except Exception as exc:  # noqa: BLE001 - a read-only disk must not lose the result
            job["files"] = {"error": f"{type(exc).__name__}: {exc}"}
        job["summary"] = summary
        job["status"] = "done"
    except Exception as exc:  # noqa: BLE001
        job["status"] = "failed"
        job["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        job["finished_at"] = time.time()


def start_job(params: Dict[str, Any], *, wait: bool = False) -> Dict[str, Any]:
    p = validate(params)
    job_id = uuid.uuid4().hex[:12]
    job = {"job_id": job_id, "status": "running", "mode": "live" if p["live"] else "deterministic",
           "case_ids": p["case_ids"], "started_at": time.time()}
    with _LOCK:
        _JOBS[job_id] = job
        while len(_JOBS) > _MAX_JOBS:
            _JOBS.pop(next(iter(_JOBS)))
    t = threading.Thread(target=_execute, args=(job_id, p), name=f"injection-eval-{job_id}", daemon=True)
    t.start()
    if wait:
        t.join()
    return _public(job)


def get_job(job_id: str, *, full: bool = False) -> Optional[Dict[str, Any]]:
    job = _JOBS.get(job_id)
    if job is None:
        return None
    return dict(job) if full else _public(job)


def latest_report(*, job_id: Optional[str] = None, mode: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """The full summary of a job, or the newest persisted report."""
    if job_id:
        job = _JOBS.get(job_id)
        return job.get("summary") if job else None
    done = [j for j in _JOBS.values() if j.get("summary") and (not mode or j["mode"] == mode)]
    if done:
        return max(done, key=lambda j: j.get("finished_at", 0))["summary"]
    try:
        files: List[str] = sorted(glob.glob(os.path.join(_reports_dir(), "*.json")))
    except Exception:  # noqa: BLE001
        return None
    for path in reversed(files):
        if mode and not path.endswith(f"-{mode}.json"):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            continue
    return None
