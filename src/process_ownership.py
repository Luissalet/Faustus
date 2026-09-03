"""Whether a pid is still this application's to kill.

``taskkill /T /F`` and ``killpg(pgid, SIGKILL)`` are unconditional: whatever
holds the pid at that instant dies, together with its children, with no prompt
and no undo. Every kill in this application is aimed at a process it started
itself — the agent shell, a test runner, an external worker — but "we started
it" is a claim about the past, and a pid is only ever on loan. Once the OS has
reaped our child the number goes back in the pool, and the next holder can be
the operator's own Ollama with two GPUs of models resident; taking that down
mid-generation is not something a retry fixes.

So ownership is asked in the present tense here, and the answer is the process
object itself. A ``subprocess.Popen`` / ``asyncio.subprocess.Process`` we hold
that has not yet reported an exit status has not been reaped, so its pid is
still bound to the child we spawned — that, and only that, authorises a kill.
A bare pid is refused wherever it comes from, and there is deliberately no
argument, flag or setting that turns the refusal off: the caller upstream of
these tools is a language model, and a safeguard a model can switch off is not
a safeguard.

``note_started`` adds a second, best-effort check for the processes that
register: the OS creation time recorded at spawn must still match the pid's
creation time at kill. That closes the narrow window in which a child has been
reaped but its object has not yet recorded the exit status.

Known residual, Windows only: ``taskkill /T`` walks *recorded* parent pids, and
Windows never clears the parent pid of an orphan. A process whose real parent
exited long ago, and whose recorded parent pid was later recycled into one of
ours, therefore looks like our descendant to taskkill. Nothing this module can
check prevents that; only a creation-time-filtered walk of the tree would, at
the price of making psutil load-bearing on the kill path.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from core.platform_compat import IS_WINDOWS

# Two creation times this far apart are two different processes. The slack
# absorbs the clock resolution psutil reports on Windows, which is coarser than
# the value we stored at spawn.
_CREATE_TIME_SLACK_S = 1.0

# pid -> (creation time when we spawned it, the command, for the refusal text)
_started: Dict[int, Tuple[Optional[float], str]] = {}
_lock = threading.Lock()


@dataclass(frozen=True)
class Ownership:
    """Whether `pid` may be killed, and — when not — why, in one word."""

    owned: bool
    pid: Optional[int]
    code: str = ""      # "" | no_process | bare_pid | exited | recycled
    reason: str = ""


def _pid_of(proc: Any) -> Optional[int]:
    pid = getattr(proc, "pid", None)
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    return pid


def _create_time(pid: int) -> Optional[float]:
    """The OS creation time of `pid`, or None when it cannot be read.

    psutil is an optional dependency here on purpose: a missing psutil must
    weaken the recycled-pid check, never break a kill.
    """
    try:
        import psutil

        return float(psutil.Process(int(pid)).create_time())
    except Exception:
        return None


def note_started(proc: Any, command: str = "") -> None:
    """Record that this application spawned `proc`. Best effort, never raises."""
    pid = _pid_of(proc)
    if pid is None:
        return
    with _lock:
        _started[pid] = (_create_time(pid), str(command or "")[:200])


def forget(proc_or_pid: Any) -> None:
    """Drop a spawn record once the process is finished with."""
    pid = proc_or_pid if isinstance(proc_or_pid, int) else _pid_of(proc_or_pid)
    if pid is None:
        return
    with _lock:
        _started.pop(pid, None)


def started_pids() -> Tuple[int, ...]:
    with _lock:
        return tuple(_started)


def check(proc: Any) -> Ownership:
    """Decide whether `proc` is still ours to kill."""
    if isinstance(proc, int) and not isinstance(proc, bool):
        # A number is not provenance. Whatever produced it — a port lookup, a
        # process listing, a model's guess — cannot show that we started it.
        return Ownership(
            False, proc if proc > 0 else None, "bare_pid",
            "a pid on its own is not evidence that Faustus started it",
        )
    pid = _pid_of(proc)
    if pid is None:
        return Ownership(False, None, "no_process", "no running process was given")
    if not hasattr(proc, "returncode"):
        return Ownership(
            False, pid, "bare_pid",
            "a pid on its own is not evidence that Faustus started it",
        )
    if proc.returncode is not None:
        return Ownership(
            False, pid, "exited",
            "the process Faustus started has already exited, so its pid "
            "may now belong to something else",
        )
    with _lock:
        record = _started.get(pid)
    if record is not None:
        spawned_at, _command = record
        current = _create_time(pid)
        if (
            spawned_at is not None
            and current is not None
            and abs(current - spawned_at) > _CREATE_TIME_SLACK_S
        ):
            return Ownership(
                False, pid, "recycled",
                "the pid has been reused since Faustus spawned it "
                "(its creation time no longer matches)",
            )
    return Ownership(True, pid, "", "")


def describe(pid: Optional[int]) -> str:
    """`name.exe (pid 1234)` when psutil can say, else `pid 1234`."""
    if not pid:
        return "that process"
    try:
        import psutil

        return f"{psutil.Process(int(pid)).name()} (pid {pid})"
    except Exception:
        return f"pid {pid}"


def manual_stop_hint(pid: Optional[int]) -> str:
    if not pid:
        return ""
    return f"taskkill /PID {pid}" if IS_WINDOWS else f"kill {pid}"


def refusal_message(verdict: Ownership) -> str:
    """What to log, and to tell the user, when a kill is refused."""
    if verdict.owned:
        return ""
    if verdict.code == "exited":
        return f"Nothing to kill: {describe(verdict.pid)} had already exited."
    return (
        f"Refusing to kill {describe(verdict.pid)}: {verdict.reason}. "
        "Faustus only stops processes it started itself. "
        + (
            f"If you meant to stop this one, run `{manual_stop_hint(verdict.pid)}` yourself."
            if verdict.pid
            else ""
        )
    ).strip()
