"""One owner for every task this process starts in the background.

Shutdown used to be a list of individual goodbyes: the upload cleaner was
cancelled, the scheduler was stopped, MCP was disconnected -- and the eight
other tasks startup had created (backups, the MCP connect, warmups, keepalive,
the null-owner sweep, the nightly skill audit, the Cookbook serve lifecycle,
the background-job monitor) were left running on an event loop that was about
to be torn down. Two consequences follow, and neither is theoretical: a task
still inside ``mcp_manager.connect_all_enabled()`` reconnects the servers that
``disconnect_all()`` has just closed, and a backup or a skill audit keeps
writing under ``data/`` while the machinery it writes through is already being
disposed of. A half-written backup is worse than no backup.

The fix is ownership, not more goodbyes. Everything long-lived is spawned
through a supervisor, so shutdown has one place to ask "what is still running?"
and one place to stop it. The order the supervisor imposes is the part that
matters: stop admitting new work first (a task spawned during teardown is a
task nobody is left to wait for), then give the work already in flight a
bounded chance to finish, then cancel what remains and *await* it -- a
``Task.cancel()`` that is never awaited has not stopped anything, it has only
asked, and the loop closing underneath swallows the difference.

``spawn`` after ``stop_accepting()`` closes the coroutine object it was handed
rather than dropping it: an un-awaited coroutine surfaces as a RuntimeWarning
at some arbitrary later GC, which is exactly the noise that hides a real leak
during shutdown.

Tasks still alive at the drain deadline are returned by name from ``close()``
so the caller can log them as interrupted. Work cut off mid-flight is a fact
the next boot reasons about (src/agent_runs.py, src/crash_recovery.py), and
silence would make a killed backup indistinguishable from one that never ran.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, List, Optional, Set

logger = logging.getLogger(__name__)

# How long shutdown lets in-flight background work finish before it is
# cancelled. Long enough for an HTTP delivery or a database commit already in
# progress; far too short for a backup, which is why backups are expected to be
# reported as interrupted rather than waited for.
DEFAULT_DRAIN_TIMEOUT_S = 5.0


def task_name(task: Any) -> str:
    """A stable label for logs, even for a task nobody named."""
    try:
        name = task.get_name()
    except Exception:
        name = ""
    return str(name or repr(task))


class TaskSupervisor:
    """Owns background tasks so that shutdown can find, drain and stop them.

    One instance per process is the intended shape (``app.background_supervisor``).
    Every method is safe to call twice: shutdown paths run under exception
    handlers and a lifespan can be entered again in the same process (tests,
    ``uvicorn --reload``), so a supervisor that could only be closed once would
    turn a second startup into a silent no-op.
    """

    def __init__(self, name: str = "app") -> None:
        self._name = name
        self._tasks: Set[asyncio.Task] = set()
        self._accepting = True
        self._interrupted: List[str] = []

    # ── lifecycle ───────────────────────────────────────────────────────
    def start(self) -> "TaskSupervisor":
        """Re-open the supervisor for a (re)start. Idempotent."""
        self._accepting = True
        self._interrupted = []
        self._forget_finished()
        return self

    def stop_accepting(self) -> None:
        """Refuse new work. The first step of shutdown, and only that: tasks
        already running are untouched so ``drain`` can still let them land."""
        self._accepting = False

    @property
    def accepting(self) -> bool:
        return self._accepting

    @property
    def interrupted(self) -> List[str]:
        """Names cancelled by the last ``close()`` because they outlived the
        drain deadline."""
        return list(self._interrupted)

    # ── registration ────────────────────────────────────────────────────
    def spawn(self, coro: Any, *, name: str) -> Optional[asyncio.Task]:
        """Start `coro` as an owned task, or refuse it once shutdown began."""
        if not self._accepting:
            close = getattr(coro, "close", None)
            if callable(close):
                # Not merely tidy: an abandoned coroutine raises
                # "coroutine ... was never awaited" from a later GC, mixed into
                # whatever is running then.
                close()
            logger.info("%s: not starting %s -- shutting down", self._name, name)
            return None
        task = asyncio.ensure_future(coro)
        return self.adopt(task, name=name)

    def adopt(self, task: Optional[asyncio.Task], *, name: str = "") -> Optional[asyncio.Task]:
        """Take ownership of a task somebody else created (a service whose
        ``start()`` returns its own loop task)."""
        if task is None:
            return None
        if name:
            try:
                task.set_name(f"{self._name}:{name}")
            except Exception:
                pass
        self._tasks.add(task)
        task.add_done_callback(self._on_done)
        return task

    def _on_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        try:
            exc = task.exception()
        except Exception:
            return
        if exc is not None:
            # Retrieved here so a crashed background task is reported once,
            # named, instead of arriving as an "exception was never retrieved"
            # traceback with no context at interpreter exit.
            logger.warning("%s: background task %s failed: %r",
                           self._name, task_name(task), exc)

    # ── inspection ──────────────────────────────────────────────────────
    def live(self) -> List[asyncio.Task]:
        return [t for t in self._tasks if not t.done()]

    def live_names(self) -> List[str]:
        return sorted(task_name(t) for t in self.live())

    def _forget_finished(self) -> None:
        for task in [t for t in self._tasks if t.done()]:
            self._tasks.discard(task)

    # ── shutdown ────────────────────────────────────────────────────────
    async def drain(self, deadline: float) -> List[asyncio.Task]:
        """Let running tasks finish on their own until `deadline`
        (a ``time.monotonic()`` stamp). Returns those still running."""
        pending = self.live()
        if not pending:
            return []
        remaining = deadline - time.monotonic()
        if remaining > 0:
            await asyncio.wait(pending, timeout=remaining)
        return self.live()

    async def close(self, drain_timeout: float = DEFAULT_DRAIN_TIMEOUT_S) -> List[str]:
        """Stop accepting, drain, then cancel and await the rest.

        Returns the names of the tasks that had to be cancelled, i.e. the work
        the caller should record as interrupted.
        """
        self.stop_accepting()
        survivors = await self.drain(time.monotonic() + max(0.0, float(drain_timeout)))
        names = sorted(task_name(t) for t in survivors)
        for task in survivors:
            task.cancel()
        if survivors:
            # Awaiting is what makes the cancellation real: without it the
            # tasks are merely flagged, and the loop shuts down around them.
            await asyncio.gather(*survivors, return_exceptions=True)
        self._forget_finished()
        self._interrupted = names
        return names
