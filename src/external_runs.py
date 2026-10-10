"""Restart-safe records of external agent runs (OBJ-53, Ágora #232).

A dispatched external agent (a CLI such as an OpenCode or Codex run) is a
real OS process. Its output goes to a log file and this module keeps one small
JSON record per run next to it: the pid with its OS creation time, the runner,
whether it was gated, and how it ended. That is what lets a Faustus that
restarted while the agent was still working find it again, follow its log and
learn when it ended, instead of calling the job "interrupted" while the agent
keeps writing in the workspace (and inviting a second run to start over it).

Identity is (pid, creation time): a bare pid can belong to another process
after a reboot, and adopting that would be worse than not adopting at all.
"""

from __future__ import annotations

import codecs
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_RUN_ID = re.compile(r"[A-Za-z0-9._-]{1,80}")
#: Slack between the creation time recorded at spawn and the one read later.
_CREATE_SLACK_S = 1.0
KEEP_RUNS = 300


def runs_dir() -> str:
    from src import constants
    return os.path.join(constants.DATA_DIR, "external_runs")


def valid_run_id(run_id: Any) -> bool:
    return bool(run_id) and bool(_RUN_ID.fullmatch(str(run_id)))


def log_path(run_id: str) -> str:
    return os.path.join(runs_dir(), f"{run_id}.log")


def _meta_path(run_id: str) -> str:
    return os.path.join(runs_dir(), f"{run_id}.json")


def _write(run_id: str, record: Dict[str, Any]) -> None:
    os.makedirs(runs_dir(), exist_ok=True)
    tmp = _meta_path(run_id) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(record, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, _meta_path(run_id))


def load(run_id: str) -> Optional[Dict[str, Any]]:
    if not valid_run_id(run_id):
        return None
    try:
        with open(_meta_path(run_id), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _creation_time(pid: int) -> Optional[float]:
    try:
        from src import process_ownership
        return process_ownership.creation_time(pid)
    except Exception:  # noqa: BLE001
        return None


def start(run_id: str, *, pid: int, runner: str, label: str = "", gated: bool = False,
          cwd: Optional[str] = None) -> None:
    """Record a run the moment its process exists. Never raises."""
    try:
        _write(run_id, {"run_id": run_id, "pid": int(pid), "created": _creation_time(int(pid)),
                        "runner": str(runner or ""), "label": str(label or ""), "gated": bool(gated),
                        "cwd": str(cwd or ""), "log": log_path(run_id), "started": time.time(),
                        "finished": None, "exit_code": None, "status": "running"})
        _prune()
    except Exception as exc:  # noqa: BLE001 - bookkeeping never stops a run
        logger.debug("external_runs: start record failed for %s: %s", run_id, exc)


def finish(run_id: str, *, status: str, exit_code: Optional[int] = None, adopted: bool = False) -> None:
    try:
        record = load(run_id) or {"run_id": run_id, "log": log_path(run_id)}
        record.update(finished=time.time(), status=str(status), exit_code=exit_code)
        if adopted:
            record["adopted"] = True
        _write(run_id, record)
    except Exception as exc:  # noqa: BLE001
        logger.debug("external_runs: finish record failed for %s: %s", run_id, exc)


def alive(record: Optional[Dict[str, Any]]) -> bool:
    """True only when the recorded process is still the SAME process."""
    if not record or record.get("finished"):
        return False
    try:
        pid = int(record.get("pid") or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    expected = record.get("created")
    try:
        import psutil
        proc = psutil.Process(pid)
        if not proc.is_running() or proc.status() == getattr(psutil, "STATUS_ZOMBIE", "zombie"):
            return False
        if expected is None:
            return False  # identity unprovable: never adopt a bare pid
        return abs(float(proc.create_time()) - float(expected)) <= _CREATE_SLACK_S
    except Exception:  # noqa: BLE001 - gone, denied, or no psutil
        return False


def live_for_job(job_id: str) -> List[Dict[str, Any]]:
    """The still-running runs of one dispatch job (run ids ``<job>-<n>``)."""
    return [r for r in unsettled_for_job(job_id) if alive(r)]


def unsettled_for_job(job_id: str) -> List[Dict[str, Any]]:
    """The runs of one dispatch job that no Faustus saw end: still running,
    or ended while Faustus was down (their log still says how)."""
    if not valid_run_id(job_id):
        return []
    out: List[Dict[str, Any]] = []
    try:
        names = os.listdir(runs_dir())
    except OSError:
        return []
    prefix = f"{job_id}-"
    for name in sorted(names):
        if not (name.startswith(prefix) and name.endswith(".json")):
            continue
        record = load(name[:-5])
        if record and not record.get("finished") and record.get("pid"):
            out.append(record)
    return out


def read_from(path: str, offset: int, limit: int = 1 << 20) -> Tuple[str, int]:
    """New text in a log since ``offset`` (bytes); complete lines only."""
    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            data = fh.read(limit)
    except OSError:
        return "", offset
    if not data:
        return "", offset
    cut = data.rfind(b"\n")
    if cut < 0:
        if len(data) < limit:
            return "", offset
        cut = len(data) - 1  # an unbounded line: hand it over in pieces
    chunk = data[:cut + 1]
    return codecs.decode(chunk, "utf-8", "replace"), offset + len(chunk)


def tail(path: str, chars: int = 4000) -> str:
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            fh.seek(max(0, size - chars * 4))
            return codecs.decode(fh.read(), "utf-8", "replace")[-chars:]
    except OSError:
        return ""


def outcome_from_log(text: str) -> Tuple[str, str]:
    """``(status, why)`` from what the agent itself printed, for a run whose
    exit code a restarted Faustus cannot observe. Only a structured result
    event counts; otherwise the answer is ``unknown``, never a guessed done."""
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") in ("result", "turn.completed", "turn.failed"):
            if event.get("type") == "turn.failed" or event.get("is_error"):
                return "error", str(event.get("subtype") or event.get("error") or "the agent reported an error")
            return "done", ""
    return "unknown", "it ended while Faustus was restarting; its exit code could not be observed"


def terminate(record: Dict[str, Any]) -> bool:
    """Stop an adopted run and its children, after checking it is still the
    same process. Returns whether anything was stopped."""
    if not alive(record):
        return False
    try:
        import psutil
        proc = psutil.Process(int(record["pid"]))
        for child in proc.children(recursive=True):
            try:
                child.kill()
            except Exception:  # noqa: BLE001
                pass
        proc.kill()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.debug("external_runs: terminate failed: %s", exc)
        return False


def _prune() -> None:
    try:
        names = [n for n in os.listdir(runs_dir()) if n.endswith(".json")]
    except OSError:
        return
    if len(names) <= KEEP_RUNS:
        return
    paths = sorted((os.path.join(runs_dir(), n) for n in names), key=os.path.getmtime)
    for path in paths[:len(paths) - KEEP_RUNS]:
        record = load(os.path.basename(path)[:-5]) or {}
        if alive(record):
            continue
        for p in (path, path[:-5] + ".log"):
            try:
                os.remove(p)
            except OSError:
                pass
