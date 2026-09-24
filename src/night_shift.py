"""night_shift.py -- an unattended queue of dispatch jobs run under a budget,
with a morning report.

A shift is a short list of task texts (up to 12) run SEQUENTIALLY, each as
its own `src/dispatch.py` job (`verify=True` by default, `fix_rounds=1`),
between two runs of the same budget check: elapsed minutes, tasks done, and
tokens spent (`dispatch.compact(job)["result"]["totals"]`) if the shift
capped that too. A run whose budget is spent between tasks stops CLEANLY --
the task in flight is allowed to finish, nothing is left half-started -- and
the shift's state says exactly why (`budget_exhausted`) rather than looking
like every task simply ran.

Persistence: one JSON file per shift, `<DATA_DIR>/night_shift/<owner>/<id>.json`,
written after every task so a crash mid-shift loses at most the task in
flight, never the ones already done. `stop(id)` sets a flag on that same
file; the running loop checks it between tasks (never mid-task -- a dispatch
job already has its own cancel path, `dispatch.cancel`, which `stop()` also
calls for the CURRENTLY running task so a stop is not a several-minute wait).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_TASKS", "STATES", "validate_spec", "start", "stop", "get", "list_for",
    "report", "latest_for",
]

MAX_TASKS = 12
STATES = ("queued", "running", "done", "stopped", "budget_exhausted", "error")
_DEFAULT_TASK_TIMEOUT_S = 1800.0

#: Owner -> shift ids currently running, so a duplicate `start()` for the
#: same owner does not silently run two shifts against the same budget
#: expectation. Not a hard lock across processes -- best-effort, like the
#: rest of this module's in-memory state.
_RUNNING: Dict[str, asyncio.Task] = {}


def _setting(key: str, default: Any) -> Any:
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:  # noqa: BLE001
        return default


def _elapsed_minutes(shift: Dict[str, Any]) -> float:
    """Minutes since `shift` started running. Its own function (rather than
    inlined `time.time()` math) so a test can control it precisely without
    monkeypatching `time.time` globally, which every other timestamp in this
    module (and plenty of unrelated code) also calls."""
    started = shift.get("started") or time.time()
    return (time.time() - started) / 60.0


def _owner_key(owner: Optional[str]) -> str:
    return str(owner or "_")


def _dir(owner: Optional[str]) -> str:
    from src.constants import DATA_DIR
    return os.path.join(DATA_DIR, "night_shift", _owner_key(owner))


def _path(owner: Optional[str], shift_id: str) -> str:
    return os.path.join(_dir(owner), f"{shift_id}.json")


def _save(shift: Dict[str, Any]) -> None:
    path = _path(shift.get("owner"), shift["id"])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        from core.atomic_io import atomic_write_json
        atomic_write_json(path, shift)
    except Exception:  # noqa: BLE001
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(shift, fh, ensure_ascii=False)


def get(owner: Optional[str], shift_id: str) -> Optional[Dict[str, Any]]:
    try:
        with open(_path(owner, shift_id), encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def list_for(owner: Optional[str], limit: int = 20) -> List[Dict[str, Any]]:
    d = _dir(owner)
    try:
        names = [n for n in os.listdir(d) if n.endswith(".json")]
    except OSError:
        return []
    rows: List[Dict[str, Any]] = []
    for n in names:
        try:
            with open(os.path.join(d, n), encoding="utf-8") as fh:
                data = json.load(fh)
                if isinstance(data, dict):
                    rows.append(data)
        except (OSError, ValueError):
            continue
    rows.sort(key=lambda s: s.get("started") or 0, reverse=True)
    return rows[: max(1, int(limit or 20))]


def latest_for(owner: Optional[str]) -> Optional[Dict[str, Any]]:
    rows = list_for(owner, limit=1)
    return rows[0] if rows else None


# ── validation ───────────────────────────────────────────────────────────

def validate_spec(raw: Any) -> Dict[str, Any]:
    """Normalise+validate a `start()` payload. Raises `ValueError`."""
    if not isinstance(raw, dict):
        raise ValueError("night_shift: a JSON object is required")
    tasks_raw = raw.get("tasks")
    if isinstance(tasks_raw, str):
        tasks_raw = [t for t in tasks_raw.split("\n")]
    if not isinstance(tasks_raw, (list, tuple)):
        raise ValueError("night_shift: 'tasks' must be a list of task descriptions")
    tasks = [str(t or "").strip() for t in tasks_raw]
    tasks = [t for t in tasks if t]
    if not tasks:
        raise ValueError("night_shift: at least one task is required")
    if len(tasks) > MAX_TASKS:
        raise ValueError(f"night_shift: at most {MAX_TASKS} tasks per shift ({len(tasks)} given)")
    workspace = str(raw.get("workspace") or "").strip()
    if not workspace:
        raise ValueError("night_shift: 'workspace' is required")
    budget_raw = raw.get("budget") if isinstance(raw.get("budget"), dict) else {}
    raw_minutes = budget_raw.get("max_minutes")
    try:
        max_minutes = int(raw_minutes) if raw_minutes not in (None, "") \
            else int(_setting("night_shift_default_max_minutes", 120))
    except (TypeError, ValueError):
        raise ValueError("night_shift: budget.max_minutes must be an integer")
    if max_minutes <= 0:
        raise ValueError("night_shift: budget.max_minutes must be > 0")
    raw_tasks = budget_raw.get("max_tasks")
    try:
        max_tasks = int(raw_tasks) if raw_tasks not in (None, "") \
            else int(_setting("night_shift_default_max_tasks", 8))
    except (TypeError, ValueError):
        raise ValueError("night_shift: budget.max_tasks must be an integer")
    if max_tasks <= 0:
        raise ValueError("night_shift: budget.max_tasks must be > 0")
    max_tokens = budget_raw.get("max_tokens")
    if max_tokens not in (None, ""):
        try:
            max_tokens = int(max_tokens)
        except (TypeError, ValueError):
            raise ValueError("night_shift: budget.max_tokens must be an integer")
        if max_tokens <= 0:
            raise ValueError("night_shift: budget.max_tokens must be > 0")
    else:
        max_tokens = None
    return {
        "tasks": tasks, "workspace": workspace,
        "model": str(raw.get("model") or "").strip() or None,
        "verify": bool(raw.get("verify", True)),
        "budget": {"max_minutes": max_minutes, "max_tasks": max_tasks, "max_tokens": max_tokens},
    }


# ── lifecycle ────────────────────────────────────────────────────────────

def start(owner: Optional[str], raw_spec: Dict[str, Any]) -> Dict[str, Any]:
    """Validate `raw_spec`, persist a queued shift, launch it in the
    background, and return the shift dict at once (state `queued`)."""
    spec = validate_spec(raw_spec)
    shift = {
        "id": uuid.uuid4().hex[:12], "owner": _owner_key(owner), "workspace": spec["workspace"],
        "tasks": spec["tasks"], "budget": spec["budget"], "model": spec["model"],
        "verify": spec["verify"], "started": None, "finished": None, "state": "queued",
        "results": [], "stop_requested": False, "created": time.time(),
    }
    _save(shift)
    task = asyncio.ensure_future(_run(shift["id"], owner))
    _RUNNING[shift["id"]] = task
    return shift


def stop(owner: Optional[str], shift_id: str) -> bool:
    """Ask a queued/running shift to stop after its current task. Returns
    False when the shift does not exist or has already finished."""
    shift = get(owner, shift_id)
    if shift is None or shift.get("state") not in ("queued", "running"):
        return False
    shift["stop_requested"] = True
    _save(shift)
    return True


async def _run(shift_id: str, owner: Optional[str]) -> None:
    from src import dispatch
    shift = get(owner, shift_id)
    if shift is None:
        return
    shift["state"] = "running"
    shift["started"] = time.time()
    _save(shift)

    budget = shift["budget"]
    max_minutes = budget.get("max_minutes")
    max_tasks = budget.get("max_tasks")
    max_tokens = budget.get("max_tokens")
    tokens_used = 0
    tasks_done = 0
    final_state = "done"
    skipped: List[str] = []

    try:
        for task_text in shift["tasks"]:
            cur = get(owner, shift_id)
            if cur is not None and cur.get("stop_requested"):
                final_state = "stopped"
                skipped = shift["tasks"][tasks_done:]
                break
            elapsed_min = _elapsed_minutes(shift)
            if max_minutes and elapsed_min >= max_minutes:
                final_state = "budget_exhausted"
                skipped = shift["tasks"][tasks_done:]
                break
            if max_tasks and tasks_done >= max_tasks:
                final_state = "budget_exhausted"
                skipped = shift["tasks"][tasks_done:]
                break
            if max_tokens and tokens_used >= max_tokens:
                final_state = "budget_exhausted"
                skipped = shift["tasks"][tasks_done:]
                break

            body: Dict[str, Any] = {
                "tasks": [{"instruction": task_text}], "workspace": shift["workspace"],
                "verify": "auto" if shift.get("verify", True) else "none", "fix_rounds": 1,
            }
            if shift.get("model"):
                body["model"] = shift["model"]
            row: Dict[str, Any] = {"task": task_text}
            try:
                job = await dispatch.start(owner, body)
                remaining_s = _DEFAULT_TASK_TIMEOUT_S
                if max_minutes:
                    remaining_s = max(60.0, (max_minutes - elapsed_min) * 60.0)
                await dispatch.wait(job, min(remaining_s, _DEFAULT_TASK_TIMEOUT_S))
                compact = dispatch.compact(job)
                res = compact.get("result") or {}
                totals = res.get("totals") or {}
                tokens_used += int(totals.get("input_tokens") or 0) + int(totals.get("output_tokens") or 0)
                row.update({
                    "job_id": job.id, "status": job.status, "verdict": compact.get("verdict"),
                    "files_changed": res.get("files_changed") or [],
                    "verification": res.get("verification"),
                    "needs_attention": job.status not in ("done",),
                })
            except Exception as exc:  # noqa: BLE001 - one task failing must not sink the shift
                logger.warning("night_shift %s: task %r failed to dispatch: %s", shift_id, task_text[:80], exc)
                row.update({"status": "error", "error": str(exc)[:400], "needs_attention": True})
            shift = get(owner, shift_id) or shift
            shift.setdefault("results", []).append(row)
            tasks_done += 1
            _save(shift)
    except asyncio.CancelledError:
        final_state = "stopped"
        raise
    finally:
        shift = get(owner, shift_id) or shift
        shift["finished"] = time.time()
        shift["state"] = final_state
        if skipped:
            shift["skipped"] = skipped
        _save(shift)
        _RUNNING.pop(shift_id, None)
        try:
            from src import notifications
            attn = sum(1 for r in shift.get("results", []) if r.get("needs_attention"))
            notifications.emit(
                "night_shift_finished", owner=owner,
                title="Night shift finished",
                body=f"{len(shift.get('results', []))} task(s) run, {attn} need attention "
                     f"({final_state}).",
                data={"shift_id": shift_id, "state": final_state},
            )
        except Exception:  # noqa: BLE001 - a notification failing must not raise here
            logger.debug("night_shift %s: notification emit failed", shift_id, exc_info=True)


# ── report ───────────────────────────────────────────────────────────────

def report(owner: Optional[str], shift_id: str) -> str:
    """A morning-readable Markdown report for one shift."""
    shift = get(owner, shift_id)
    if shift is None:
        return f"# Night shift\n\nNo shift found with id `{shift_id}`."
    lines = [f"# Night shift `{shift['id']}`", ""]
    lines.append(f"- Workspace: `{shift.get('workspace')}`")
    lines.append(f"- State: **{shift.get('state')}**")
    started, finished = shift.get("started"), shift.get("finished")
    if started and finished:
        lines.append(f"- Duration: {round((finished - started) / 60.0, 1)} minutes")
    budget = shift.get("budget") or {}
    lines.append(
        f"- Budget: max {budget.get('max_minutes')} minutes, max {budget.get('max_tasks')} tasks"
        + (f", max {budget.get('max_tokens')} tokens" if budget.get("max_tokens") else "")
    )
    results = shift.get("results") or []
    lines.append(f"- Tasks run: {len(results)} / {len(shift.get('tasks') or [])}")
    lines.append("")
    lines.append("## Tasks")
    for i, row in enumerate(results, 1):
        verdict = row.get("verdict") or row.get("status") or "unknown"
        lines.append(f"\n### {i}. {row.get('task')}")
        lines.append(f"- Verdict: {verdict}")
        if row.get("job_id"):
            lines.append(f"- Job: `{row['job_id']}`")
        files = row.get("files_changed") or []
        if files:
            lines.append(f"- Files changed: {', '.join(files[:20])}")
        verification = row.get("verification")
        if isinstance(verification, dict) and verification:
            lines.append(f"- Verification: {verification.get('mode') or verification.get('label') or 'ran'}")
        if row.get("error"):
            lines.append(f"- Error: {row['error']}")
    skipped = shift.get("skipped") or []
    if skipped:
        lines.append("\n## Skipped (budget exhausted before these ran)")
        for t in skipped:
            lines.append(f"- {t}")
    attention = [r for r in results if r.get("needs_attention")]
    lines.append("\n## Needs your attention")
    if attention:
        for r in attention:
            lines.append(f"- {r.get('task')} -- {r.get('verdict') or r.get('status') or r.get('error')}")
    else:
        lines.append("- Nothing -- every task that ran finished cleanly." if results else "- Nothing ran.")
    return "\n".join(lines)
