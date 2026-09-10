"""Background job execution for the agent's `bash` tool.

Long commands (installs, ffmpeg, model downloads) should NOT block the chat
stream — a multi-minute held SSE connection is fragile (model-stops-early,
timeouts, tab suspend). Instead we launch them **detached** and let an
always-on monitor re-invoke the agent when they finish ("auto-continue").

Design goals:
  * Restart-safe: status is derived from an on-disk exit-code file, not a live
    PID, so a uvicorn restart never loses a job or its result.
  * Idempotent follow-up: a job stays {done, followed_up: False} until the
    agent has actually been re-invoked, so completion can never silently
    "do nothing" — the monitor retries on the next tick.
  * Bounded: a hard max-runtime marks a runaway job failed and STILL triggers
    a follow-up ("timed out"), so you always hear back.

This module only owns launch + state. The monitor / agent re-invocation lives
in the caller (so this stays import-light and unit-testable).
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.atomic_io import atomic_write_json
from core.platform_compat import (
    detached_popen_kwargs,
    find_bash,
    git_bash_path,
    pid_alive,
)

from src import process_ownership
from src.constants import BG_JOBS_DIR, BG_JOBS_FILE
from src.native_env import native_host_environment

logger = logging.getLogger(__name__)

_JOBS_DIR = Path(BG_JOBS_DIR)
_STORE = Path(BG_JOBS_FILE)

# A job that runs longer than this is presumed stuck and reaped (the agent
# still gets a "timed out" follow-up so nothing hangs forever).
DEFAULT_MAX_RUNTIME_S = 3600  # 1 hour
# Cap how much captured output we keep / feed back to the model.
_MAX_OUTPUT_CHARS = 16000
# How long a finished-and-followed-up job (record + its .sh/.cmd.sh/.log/.exit
# files) is kept before pruning, so neither the store nor data/bg_jobs/ grows
# without bound. The agent has already consumed the result by then.
_RETENTION_S = 3600  # 1 hour after follow-up


def _load() -> Dict[str, Dict[str, Any]]:
    try:
        if _STORE.exists():
            data = json.loads(_STORE.read_text(encoding="utf-8")) or {}
            if not isinstance(data, dict):
                return {}
            return {str(job_id): rec for job_id, rec in data.items() if isinstance(rec, dict)}
    except Exception:
        pass
    return {}


def _save(jobs: Dict[str, Dict[str, Any]]) -> None:
    atomic_write_json(str(_STORE), jobs, indent=2)


def _pid_alive(pid: Optional[int]) -> bool:
    # Delegates to the platform-safe probe. NB: a bare os.kill(pid, 0) is unsafe
    # on Windows — CPython routes it to TerminateProcess, which would KILL the
    # job we're only trying to check. core.platform_compat.pid_alive handles
    # both OSes correctly.
    return pid_alive(pid)


def _is_paused_for_resource_pressure() -> bool:
    """Whether the RAM/commit-pressure guard (src/bg_monitor.py, PERF-04) says
    auxiliary work should stand down right now.

    Imported lazily and defensively: `src/bg_monitor.py` already imports this
    module at top level (it drains bg_jobs' own follow-up queue), so importing
    it back here at module scope would be circular. Any failure to read the
    guard (psutil missing, bg_monitor not yet initialized) is "not pressured"
    -- ignorance of memory pressure must never be the reason a `#!bg` command
    silently never runs.
    """
    try:
        from src import bg_monitor
        return bool(bg_monitor.is_paused_for_resource_pressure())
    except Exception:
        return False


def _spawn_process(job_id: str, command: str, cwd: Optional[str]) -> Dict[str, Any]:
    """Actually start `command` detached and return the process-related record
    fields (status='running'). Split out of `launch()` so a job queued for RAM
    pressure (see `launch()` / `refresh()`) can be started later by the exact
    same path once pressure clears, instead of a second, divergent copy of the
    spawn logic."""
    log_path = _JOBS_DIR / f"{job_id}.log"
    exit_path = _JOBS_DIR / f"{job_id}.exit"

    # The user command goes in its OWN script file, run as a child `bash`. This
    # is what isolates it: an `exit` inside it only ends that child (so the
    # wrapper still records the exit code), and — unlike textually wrapping the
    # command in `( … )` — the wrapper can't be broken by an unbalanced paren or
    # a trailing line-continuation in the command. `$?` is the child's real
    # exit status.
    bash = find_bash()
    if bash:
        # POSIX, or Windows with Git Bash/WSL. The user command goes in its OWN
        # script file, run as a child `bash` — an `exit` inside it only ends
        # that child (so the wrapper still records the exit code), and an
        # unbalanced paren / trailing line-continuation in the command can't
        # break the wrapper. `$?` is the child's real exit status. Paths are
        # emitted as POSIX (forward-slash) + shell-quoted so Git Bash on Windows
        # handles drive paths and spaces correctly.
        cmd_path = _JOBS_DIR / f"{job_id}.cmd.sh"
        cmd_path.write_text(command + "\n", encoding="utf-8")
        lp, xp, cp = (shlex.quote(git_bash_path(p)) for p in (log_path, exit_path, cmd_path))
        script_path = _JOBS_DIR / f"{job_id}.sh"
        script_path.write_text(
            f"bash {cp} > {lp} 2>&1\n"
            f"echo $? > {xp}\n",
            encoding="utf-8",
        )
        argv = [bash, str(script_path)]
    else:
        # Windows without any bash installed: cmd.exe wrapper. The command runs
        # in its own child .cmd so %ERRORLEVEL% is the command's real exit code.
        child_path = _JOBS_DIR / f"{job_id}.child.cmd"
        child_path.write_text("@echo off\r\n" + command + "\r\n", encoding="utf-8")
        script_path = _JOBS_DIR / f"{job_id}.cmd"
        script_path.write_text(
            "@echo off\r\n"
            f'call "{child_path}" > "{log_path}" 2>&1\r\n'
            f'echo %ERRORLEVEL%> "{exit_path}"\r\n',
            encoding="utf-8",
        )
        argv = [os.environ.get("ComSpec", "cmd.exe"), "/c", str(script_path)]

    proc = subprocess.Popen(
        argv,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        cwd=cwd or None,
        # `#!bg` is the agent's Bash tool in detached form, and the commands it
        # is told to send here are installs and downloads — the very ones that
        # must not land in Faustus's virtualenv. Inheriting our environment
        # would make `#!bg pip install x` install into OUR site-packages while
        # the same command in the foreground correctly installs into theirs.
        env=native_host_environment(),
        **detached_popen_kwargs(),  # detach from the request lifecycle (setsid / DETACHED_PROCESS)
    )

    # A pid is on loan from the OS: once this detached child is reaped the
    # number goes back in the pool, and the record on disk long outlives both
    # the process and this interpreter. So the identity that gets persisted is
    # (pid, creation time) — the pair is unique for good, where the pid alone is
    # unique only while the process lives — plus the POSIX process group, which
    # is the only handle that still reaches a grandchild that re-parented to
    # init. `id` is the run identifier the audit asks for; it is already the key
    # this record is stored under. None for the creation time means psutil was
    # not importable at spawn, and `_kill_job` will then refuse to signal rather
    # than guess: see src/process_ownership.terminate_tree.
    return {
        "status": "running",       # running | done | failed
        "pid": proc.pid,
        "pid_created_at": process_ownership.creation_time(proc.pid),
        "pgid": process_ownership.process_group_id(proc.pid),
        "started_at": time.time(),
        "ended_at": None,
        "exit_code": None,
        "log_path": str(log_path),
        "exit_path": str(exit_path),
    }


def launch(command: str, session_id: str, cwd: Optional[str] = None,
           max_runtime_s: int = DEFAULT_MAX_RUNTIME_S) -> Dict[str, Any]:
    """Launch `command` detached. Returns the job record.

    Ordinarily `status='running'`: output + the final exit code are written to
    files so status survives a server restart, and the process is put in its
    own session (setsid) so it outlives the request/stream that started it.

    Under RAM/commit pressure (PERF-04, `src/bg_monitor.py`) the job is
    queued instead (`status='queued'`, nothing spawned yet) rather than
    discarded or run into an already-stressed box: `refresh()` starts it on a
    later poll once `bg_monitor.is_paused_for_resource_pressure()` clears, so
    the command still runs — just later, the same way a follow-up the monitor
    couldn't deliver this tick is simply retried next tick.
    """
    _JOBS_DIR.mkdir(parents=True, exist_ok=True)
    job_id = uuid.uuid4().hex[:12]
    rec: Dict[str, Any] = {
        "id": job_id,
        "session_id": session_id,
        "command": command,
        "max_runtime_s": max_runtime_s,
        "followed_up": False,       # has the agent been re-invoked with the result?
    }
    if _is_paused_for_resource_pressure():
        rec.update({
            "status": "queued",
            "pid": None, "pid_created_at": None, "pgid": None,
            "started_at": None, "ended_at": None, "exit_code": None,
            "log_path": None, "exit_path": None,
            "cwd": cwd,
            "queued_for_pressure": True,
        })
        logger.info("bg job %s: queued for RAM pressure instead of launched: %s",
                    job_id, command[:80])
    else:
        rec.update(_spawn_process(job_id, command, cwd))
    jobs = _load()
    jobs[job_id] = rec
    _save(jobs)
    return rec


def _read_output(rec: Dict[str, Any]) -> str:
    try:
        txt = Path(rec["log_path"]).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    if len(txt) > _MAX_OUTPUT_CHARS:
        # Keep head + tail — the interesting bits are usually at both ends.
        head = txt[: _MAX_OUTPUT_CHARS // 2]
        tail = txt[-_MAX_OUTPUT_CHARS // 2:]
        txt = head + "\n…[truncated]…\n" + tail
    return txt


def _prune(jobs: Dict[str, Dict[str, Any]], now: float) -> bool:
    """Drop records (and their on-disk files) for jobs that finished, were
    followed up, and are older than the retention window. Mutates `jobs`."""
    stale = [jid for jid, rec in jobs.items()
             if rec.get("followed_up") and rec.get("ended_at")
             and (now - rec["ended_at"]) > _RETENTION_S]
    for jid in stale:
        jobs.pop(jid, None)
        for p in _JOBS_DIR.glob(f"{jid}.*"):   # .sh .cmd.sh .log .exit
            try:
                p.unlink()
            except Exception:
                pass
    return bool(stale)


def refresh() -> Dict[str, Dict[str, Any]]:
    """Reconcile every running job against disk. Marks done/failed (incl.
    timeout); starts any job still queued for RAM pressure once the guard
    clears. Idempotent — safe to call from a poll loop (bg_monitor's tick
    already calls this indirectly via `pending_followups()`, and every status
    read — `get`/`list_for_session` — calls it directly, so a queued job does
    not need a poll loop of its own). Returns the store."""
    jobs = _load()
    changed = False
    now = time.time()
    if not _is_paused_for_resource_pressure():
        for rec in jobs.values():
            if rec.get("status") != "queued":
                continue
            rec.update(_spawn_process(rec["id"], rec.get("command", ""), rec.get("cwd")))
            changed = True
            logger.info("bg job %s: RAM pressure cleared, launching now", rec.get("id"))
    for rec in jobs.values():
        if rec.get("status") != "running":
            continue
        exit_path = Path(rec.get("exit_path", ""))
        if exit_path.exists():
            try:
                code = int(exit_path.read_text(encoding="utf-8", errors="replace").strip() or "1")
            except Exception:
                code = 1
            rec["exit_code"] = code
            rec["status"] = "done" if code == 0 else "failed"
            rec["ended_at"] = now
            changed = True
        elif (now - rec.get("started_at", now)) > rec.get("max_runtime_s", DEFAULT_MAX_RUNTIME_S):
            # Runaway / stuck — reap it but STILL surface a follow-up. The
            # reaping may be refused (see _kill_job); the follow-up is not,
            # because a job nobody hears back about is the failure this module
            # exists to prevent.
            _kill_job(rec)
            rec["status"] = "failed"
            rec["exit_code"] = -1
            rec["ended_at"] = now
            rec["timed_out"] = True
            changed = True
        elif not _pid_alive(rec.get("pid")) and not exit_path.exists():
            # Process vanished without writing an exit code (killed, OOM,
            # crash). Don't leave it "running" forever.
            rec["status"] = "failed"
            rec["exit_code"] = -1
            rec["ended_at"] = now
            rec["died"] = True
            changed = True
    if _prune(jobs, now):
        changed = True
    if changed:
        _save(jobs)
    return jobs


def _kill_job(rec: Dict[str, Any]) -> process_ownership.TreeKill:
    """Tear down a job's process tree — but only what the record can still
    prove is ours. Mutates `rec` with the outcome; never raises.

    This is the one kill path in the application that cannot hold the process
    object: the job is detached on purpose so it survives a restart, so all that
    is left is a pid read back from a JSON file, possibly written by a previous
    interpreter days ago. That is precisely the situation in which a pid means
    nothing on its own, which is why `launch` records the creation time next to
    it. When the comparison fails, or there is nothing recorded to compare
    against (a record written before this release, or a host without psutil),
    the job is marked `ownership_unknown` and NOTHING is signalled — an unkilled
    runaway costs CPU, whereas killing the wrong pid takes down whatever the
    operator happens to be running under that number.
    """
    outcome = process_ownership.terminate_tree(
        rec.get("pid"),
        spawned_at=rec.get("pid_created_at"),
        pgid=rec.get("pgid"),
    )
    if outcome.owned:
        logger.info("bg job %s: killed pid %s and %d verified descendant(s)",
                    rec.get("id"), rec.get("pid"), max(0, len(outcome.signalled) - 1))
        return outcome
    if outcome.code == "ownership_unknown":
        rec["ownership_unknown"] = True
    rec["kill_refused"] = outcome.reason
    logger.warning("bg job %s: did not signal %s — %s", rec.get("id"),
                   process_ownership.describe(rec.get("pid")), outcome.reason)
    return outcome


def pending_followups() -> List[Dict[str, Any]]:
    """Finished jobs the agent hasn't been re-invoked for yet. The monitor
    drains these; mark_followed_up() flips the flag only on success."""
    jobs = refresh()
    return [r for r in jobs.values()
            if r.get("status") in ("done", "failed") and not r.get("followed_up")]


def mark_followed_up(job_id: str) -> None:
    jobs = _load()
    if job_id in jobs:
        jobs[job_id]["followed_up"] = True
        _save(jobs)


def get(job_id: str) -> Optional[Dict[str, Any]]:
    refresh()  # reconcile against disk so status/exit_code are current
    rec = _load().get(job_id)
    if rec:
        rec = dict(rec)
        rec["output"] = _read_output(rec)
    return rec


def list_for_session(session_id: str) -> List[Dict[str, Any]]:
    return [r for r in refresh().values() if r.get("session_id") == session_id]


def kill(job_id: str) -> Optional[Dict[str, Any]]:
    """Terminate a running job's process tree and mark it killed. Returns the
    updated record, or None if the id is unknown. Idempotent: a job that already
    finished is returned unchanged, with `kill_refused` saying why nothing was
    signalled. Sets followed_up so the monitor does not also fire an
    auto-continue for a job the agent deliberately stopped."""
    # refresh() before reading, not _load(): a record still marked `running`
    # whose process died without writing its exit file would otherwise hand a
    # stored pid straight to kill_process_tree. A pid the OS has already reaped
    # is back in the pool, and killing it takes down whatever holds it now.
    # refresh() is what turns that record into `failed`, so the branch below
    # never fires for a process that is gone.
    jobs = refresh()
    rec = jobs.get(job_id)
    if rec is None:
        return None
    if rec.get("status") == "running":
        outcome = _kill_job(rec)
        rec["status"] = "failed"
        rec["exit_code"] = -1
        rec["ended_at"] = time.time()
        # `killed` says a signal was actually sent. A refused kill still ends
        # the record — leaving it `running` forever would be worse — but must
        # not claim the process is gone, because it is very likely still there.
        rec["killed"] = bool(outcome.signalled)
        rec["followed_up"] = True
        _save(jobs)
        return rec
    if rec.get("status") == "queued":
        # Nothing was ever spawned (still parked for RAM pressure, see
        # launch()/refresh()) — there is no process tree to signal, just the
        # queue slot to drop. Distinguishing this from "already finished" below
        # matters: `process_ownership.describe(None)` would otherwise read as
        # if a real process had already exited on its own.
        rec = dict(rec)
        rec["status"] = "failed"
        rec["exit_code"] = -1
        rec["ended_at"] = time.time()
        rec["killed"] = True
        rec["followed_up"] = True
        jobs[job_id] = rec
        _save(jobs)
        return rec
    # Reached only after refresh() has reconciled this record against disk, so
    # the job is genuinely over. Say that rather than returning a record the
    # caller could read as "killed" — `process_ownership.describe` names what
    # holds the pid now, which is the point: it is no longer ours.
    rec = dict(rec)
    rec["kill_refused"] = (
        f"Job {job_id} already {rec.get('status')}; nothing was signalled. "
        f"Faustus no longer owns {process_ownership.describe(rec.get('pid'))} — "
        "the OS may have handed that number to an unrelated process."
    )
    return rec


def cancel_for_session(session_id: str) -> List[Dict[str, Any]]:
    """TASK-04 `scope=work`: kill every still-running/queued bg job launched
    by `session_id`'s bash tool. Delegates one job at a time to `kill()` so
    ownership stays proven per-process (see `_kill_job`) rather than any kind
    of session-wide signal; a job already finished is left untouched (`kill()`
    reports it, never resignals it). Returns the updated record of each job
    that was still live, in the shape `kill()` already returns."""
    if not session_id:
        return []
    touched: List[Dict[str, Any]] = []
    for rec in list_for_session(session_id):
        if rec.get("status") not in ("running", "queued"):
            continue
        updated = kill(str(rec.get("id") or ""))
        if updated is not None:
            touched.append(updated)
    return touched


def result_text(rec: Dict[str, Any]) -> str:
    """Human/agent-readable summary of a finished job, for the follow-up."""
    out = _read_output(rec)
    if rec.get("killed"):
        head = "Background job was killed."
    elif rec.get("timed_out"):
        head = f"Background job timed out after {rec.get('max_runtime_s')}s."
    elif rec.get("died"):
        head = "Background job process died unexpectedly (no exit code)."
    else:
        head = f"Background job finished with exit code {rec.get('exit_code')}."
    if rec.get("kill_refused"):
        # The agent has to know the difference between "stopped" and "given up
        # on": in the second case the command is probably still running, and a
        # follow-up that assumes otherwise will make wrong decisions.
        hint = process_ownership.manual_stop_hint(rec.get("pid"))
        head += (f" Its process was NOT signalled: {rec['kill_refused']}."
                 + (f" Stop it yourself with `{hint}` if it is still running." if hint else ""))
    return f"{head}\nCommand: {rec.get('command')}\n\nOutput:\n{out or '(no output)'}"
