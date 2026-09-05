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

``terminate_tree`` extends the same question to the descendants, which is a
separate problem with a separate answer. ``taskkill /T`` walks *recorded*
parent pids, and Windows never clears the parent pid of an orphan: a process
whose real parent exited long ago, and whose recorded parent pid was later
recycled into one of ours, looks exactly like our descendant to taskkill and
dies with the tree. So the tree is walked here instead, one generation at a
time, and a "child" whose creation time precedes the parent it hangs off is
refused and never expanded -- nothing can descend from a process that did not
exist yet. psutil is load-bearing for that walk; that is the price. Without it,
a caller that still holds a live process object may fall back to the old
unconditional spelling (it has other evidence), and a caller holding nothing
but a pid read back from disk (src/bg_jobs.py) refuses to signal at all.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from core.platform_compat import IS_WINDOWS

logger = logging.getLogger(__name__)

# The signal a verified teardown sends. SIGKILL is the POSIX spelling and the
# only one that cannot be trapped; the getattr keeps the module importable — and
# the POSIX branch exercisable under test — on Windows, where the constant does
# not exist and psutil's own kill() is a TerminateProcess anyway.
_KILL_SIGNAL = getattr(signal, "SIGKILL", None) or getattr(signal, "SIGTERM", 15)

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


# Latched so a host without psutil pays the failed import once, not on every
# spawn: note_started runs on the hot path of every bash/python tool call.
_psutil_module: Any = None
_psutil_probed = False


def _psutil() -> Any:
    """The psutil module, or None. Optional on purpose: a missing psutil must
    weaken the recycled-pid check, never break a kill."""
    global _psutil_module, _psutil_probed
    if not _psutil_probed:
        try:
            import psutil

            _psutil_module = psutil
        except Exception:
            _psutil_module = None
        _psutil_probed = True
    return _psutil_module


def _create_time(pid: int) -> Optional[float]:
    """The OS creation time of `pid`, or None when it cannot be read."""
    psutil = _psutil()
    if psutil is None:
        return None
    try:
        return float(psutil.Process(int(pid)).create_time())
    except Exception:
        return None


def creation_time(pid: Optional[int]) -> Optional[float]:
    """The OS creation time to persist alongside a pid at spawn.

    A pid identifies a process only while that process lives; (pid, creation
    time) identifies it for good. Callers that outlive the process object --
    anything that writes a pid to disk and reads it back after a restart --
    have nothing else to compare against later, so recording this at spawn is
    what makes ownership provable at all. None when psutil is unavailable, and
    the caller must then treat ownership as unprovable rather than assumed.
    """
    if not pid:
        return None
    return _create_time(int(pid))


def process_group_id(pid: Optional[int]) -> Optional[int]:
    """The POSIX process group of `pid`; None on Windows or when unreadable.

    Worth persisting next to the pid: children that re-parent to init drop off
    the parent/child walk entirely, but they stay in the process group their
    setsid'd ancestor created, so the group is the only handle that still
    reaches them.
    """
    getpgid = getattr(os, "getpgid", None)
    if not pid or getpgid is None:
        return None
    try:
        return int(getpgid(int(pid)))
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


def spawn_creation_time(pid: Optional[int]) -> Optional[float]:
    """The creation time `note_started` recorded for `pid`, if any."""
    if not pid:
        return None
    with _lock:
        record = _started.get(int(pid))
    return record[0] if record else None


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


@dataclass(frozen=True)
class TreeKill:
    """What a tree teardown signalled, and what it refused to touch.

    Returned rather than raised: every caller is a cleanup path that has to keep
    going, and the interesting outcome (`nothing was killed, because the pid is
    not ours any more`) is not an error — it is the result.
    """

    code: str = ""      # "" | no_process | gone | recycled | ownership_unknown
    reason: str = ""
    signalled: Tuple[int, ...] = ()
    rejected: Tuple[Tuple[int, str], ...] = ()
    group_signalled: Optional[int] = None

    @property
    def owned(self) -> bool:
        return self.code == ""


def _same_creation(a: Optional[float], b: Optional[float]) -> bool:
    return a is not None and b is not None and abs(a - b) <= _CREATE_TIME_SLACK_S


def _unverified_tree_kill(pid: int) -> TreeKill:
    """The pre-psutil spelling, kept only for hosts without psutil.

    Reached only when the caller has independent evidence of ownership (it is
    holding the unreaped process object) and psutil cannot be imported to check
    the descendants. Leaving the tree alive was the original bug — a foreground
    `uvicorn` that outlived every turn — so the unconditional walk stays as the
    fallback, with its known Windows residual, rather than being turned off.
    """
    import subprocess

    if IS_WINDOWS:
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                capture_output=True, timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as e:
            logger.debug("taskkill fallback for pid %s failed: %s", pid, e)
        return TreeKill("", "psutil unavailable: killed the recorded tree unverified",
                        signalled=(pid,))
    group = process_group_id(pid)
    own_group = process_group_id(os.getpid())
    if group is not None and group != own_group:
        try:
            os.killpg(group, _KILL_SIGNAL)
            return TreeKill("", "psutil unavailable: killed the process group unverified",
                            signalled=(pid,), group_signalled=group)
        except Exception as e:
            logger.debug("killpg fallback for group %s failed: %s", group, e)
    try:
        os.kill(pid, _KILL_SIGNAL)
    except Exception as e:
        logger.debug("kill fallback for pid %s failed: %s", pid, e)
    return TreeKill("", "psutil unavailable: killed the root unverified", signalled=(pid,))


def terminate_tree(
    pid: Optional[int],
    *,
    spawned_at: Optional[float] = None,
    pgid: Optional[int] = None,
    unverified_tree_ok: bool = False,
) -> TreeKill:
    """Kill `pid` and the descendants that can be *shown* to be its own.

    `spawned_at` is the creation time recorded when this application launched
    the process (`creation_time`). It is the whole proof: if the number now
    holds a process created at a different moment, the OS recycled the pid and
    whatever is there belongs to somebody else — on this machine, plausibly the
    operator's Ollama with two GPUs of models resident. No signal is sent then,
    and none is sent when nothing was recorded to compare against, unless the
    caller sets `unverified_tree_ok` because it holds the live process object
    itself and therefore already knows the pid was never released.

    `pgid` is the POSIX process group recorded at spawn; when it still matches,
    the group is signalled too, since a re-parented grandchild is no longer
    reachable by walking children but is still in the group.
    """
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return TreeKill("no_process", "no pid to signal")
    psutil = _psutil()
    if psutil is None:
        if not unverified_tree_ok:
            return TreeKill(
                "ownership_unknown",
                "psutil is not installed, so the creation time of this pid "
                "cannot be compared with the one recorded when it was spawned",
            )
        return _unverified_tree_kill(pid)
    try:
        root = psutil.Process(pid)
        root_created = float(root.create_time())
    except Exception:
        return TreeKill("gone", "no process is holding that pid any more")
    if spawned_at is None:
        if not unverified_tree_ok:
            return TreeKill(
                "ownership_unknown",
                "no creation time was recorded when this process was spawned, "
                "so there is nothing to prove the pid still holds it",
            )
    elif not _same_creation(root_created, spawned_at):
        logger.warning(
            "Refusing to kill %s: created at %.3f, but the process Faustus "
            "spawned under that pid was created at %.3f — the pid was reused",
            describe(pid), root_created, spawned_at,
        )
        return TreeKill(
            "recycled",
            "the pid has been reused since Faustus spawned it "
            "(its creation time no longer matches)",
        )

    accepted, rejected = _walk_owned_descendants(root, root_created)
    for child, why in rejected:
        # The point of the walk: say out loud which process was spared and why,
        # because the alternative spelling would have killed it silently.
        logger.warning("Not killing %s while tearing down pid %s: %s",
                       describe(child), pid, why)
    signalled: List[int] = []
    # Leaves first: killing the root first re-parents its children, and a
    # re-parented child is exactly the case this walk refuses to touch.
    for proc in reversed(accepted):
        if _kill_one(proc):
            signalled.append(proc.pid)
    if _kill_one(root):
        signalled.append(pid)
    group_signalled = None
    if pgid is not None and not IS_WINDOWS:
        current_group = process_group_id(pid)
        own_group = process_group_id(os.getpid())
        if current_group == pgid and pgid != own_group:
            try:
                os.killpg(int(pgid), _KILL_SIGNAL)
                group_signalled = int(pgid)
            except Exception as e:
                logger.debug("killpg %s failed: %s", pgid, e)
    logger.info("Killed %s and %d verified descendant(s); refused %d",
                describe(pid), max(0, len(signalled) - 1), len(rejected))
    return TreeKill("", "", tuple(signalled), tuple(rejected), group_signalled)


def _walk_owned_descendants(root: Any, root_created: float) -> Tuple[List[Any], List[Tuple[int, str]]]:
    """Breadth-first walk of `root`'s children, pruning what cannot be ours.

    Deliberately not ``children(recursive=True)``: that expands every recorded
    parent link, so one falsely-attributed orphan drags its own real subtree in
    with it. Here a rejected process is never expanded.
    """
    accepted: List[Any] = []
    rejected: List[Tuple[int, str]] = []
    frontier = [(root, root_created)]
    seen = {root.pid}
    while frontier:
        parent, parent_created = frontier.pop(0)
        try:
            children = parent.children()
        except Exception:
            continue
        for child in children:
            if child.pid in seen:
                continue
            seen.add(child.pid)
            try:
                child_created = float(child.create_time())
            except Exception:
                rejected.append((child.pid, "its creation time could not be read"))
                continue
            if child_created + _CREATE_TIME_SLACK_S < parent_created:
                rejected.append((
                    child.pid,
                    "it is older than the process it is recorded under, so it "
                    "cannot be a descendant of it — the parent pid was recycled",
                ))
                continue
            accepted.append(child)
            frontier.append((child, child_created))
    return accepted, rejected


def _kill_one(proc: Any) -> bool:
    try:
        proc.kill()
        return True
    except Exception as e:
        logger.debug("kill %s failed: %s", getattr(proc, "pid", "?"), e)
        return False


def describe(pid: Optional[int]) -> str:
    """`name.exe (pid 1234)` when psutil can say, else `pid 1234`."""
    if not pid:
        return "that process"
    psutil = _psutil()
    if psutil is not None:
        try:
            return f"{psutil.Process(int(pid)).name()} (pid {pid})"
        except Exception:
            pass
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
