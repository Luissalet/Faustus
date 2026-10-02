"""Background work for the apps: a lane scheduler for periodic/one-off jobs and a persisted job queue.

Standard library only (``JobQueue`` needs a :class:`hoard_link.sqlkit.Database`). The Node twin of the periodic part
is ``startBackground`` in ``js/hoard-commons/server.js``.

Two tools, because the apps had two kinds of background work:

* :class:`LaneScheduler` is the ``scheduler.py`` that Kafka, Phileas, Tantalus and Galton each carried (about 65 %
  identical): *lanes* (named groups of worker threads, one job at a time per worker), jobs deduplicated by key,
  periodic jobs that become due, ``run_now`` that waits for a result (or runs inline when the lanes are not
  started, for tests and MCP-only mode), a ``status()`` view. The addition: **the last-run times can be saved**
  (``state_path``), so a restart does not find every job overdue and run them all at once. Time is injectable.
* :class:`JobQueue` is the persisted queue (Lumiere ``jobs.py``, Prospero ``requeue_running_jobs``, Daguerre
  ``interrupted``): jobs live in a SQLite table, have progress and a cooperative cancel, survive a restart (what was
  running becomes ``interrupted`` or is queued again, per kind) and may say "not enough resources now" by raising
  :class:`WaitingForResources`.

A job that raises is logged and never stops its lane.
"""

from __future__ import annotations

import dataclasses
import logging
import queue
import re
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Union

from . import atomic
from .ids import new_id
from .sqlkit import Database, dumps, loads
from .waiting import MAX_WAIT_S, wait_for

__all__ = [
    "Job", "LaneScheduler", "JobQueue", "JobCtx", "JobCancelled", "JobNotFound", "WaitingForResources", "jobs_schema",
]

log = logging.getLogger("hoard_link.lanes")


# ================================================================== LaneScheduler

@dataclass(eq=False)
class Job:
    """One unit of work for a :class:`LaneScheduler`.

    ``fn`` is called with no arguments (bind arguments with ``functools.partial`` or a closure). ``key`` is the
    deduplication key (default: ``kind``): while a job with the same key is queued or running, submitting another is a
    no-op. ``lane`` names the lane that runs it. ``every_s`` (a number, or a callable returning one, for settings that
    change at runtime) makes a registered job periodic; ``first_delay_s`` is how long after the scheduler started it
    first becomes due when it has never run. ``enabled`` is an optional predicate checked before a periodic run is
    queued. ``reason`` is free text shown in ``status()`` (``schedule``, ``manual``...).
    """

    kind: str
    key: Optional[str] = None
    fn: Optional[Callable[[], Any]] = None
    lane: str = "default"
    every_s: Union[float, Callable[[], float], None] = None
    first_delay_s: float = 0.0
    enabled: Optional[Callable[[], bool]] = None
    reason: str = "schedule"
    # filled in while it runs
    done: threading.Event = field(default_factory=threading.Event, init=False, repr=False, compare=False)
    result: Any = field(default=None, init=False, repr=False, compare=False)
    error: str = field(default="", init=False, repr=False, compare=False)

    @property
    def dedupe_key(self) -> str:
        return self.key if self.key is not None else self.kind


class LaneScheduler:
    """Named lanes of worker threads plus periodic jobs.

    ``lanes`` maps a lane name to its number of workers (``{"ingest": 1, "reminders": 1}``). ``state_path``
    (optional) is a JSON file where the last-run times are kept. ``paused`` (a callable) holds back only the
    *automatic* jobs: a person or an app that asks for ``run_now`` is never held back. ``on_error(job, exc)`` is
    called (and its own failures ignored) when a job raises. ``clock`` is the wall clock (``time.time``) and can be
    replaced in tests; ``tick_s`` is how often the scheduler looks for due jobs.
    """

    def __init__(self, lanes: Mapping[str, int], *, state_path: Union[str, Path, None] = None, enabled: bool = True,
                 paused: Callable[[], bool] = lambda: False, on_error: Optional[Callable[[Job, BaseException], None]] = None,
                 clock: Callable[[], float] = time.time, tick_s: float = 20.0, name: str = "lanes"):
        if not lanes:
            raise ValueError("a scheduler needs at least one lane")
        self.lanes: dict[str, int] = {str(k): max(1, int(v)) for k, v in lanes.items()}
        self.state_path = Path(state_path) if state_path else None
        self.enabled = enabled
        self.paused = paused
        self.on_error = on_error
        self.clock = clock
        self.tick_s = tick_s
        self.name = name
        self._registered: dict[str, Job] = {}
        self._queues: dict[str, "queue.Queue[Optional[Job]]"] = {lane: queue.Queue() for lane in self.lanes}
        self._pending: set[str] = set()
        self._current: dict[str, list[Job]] = {lane: [] for lane in self.lanes}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._born = clock()
        self.last: dict[str, float] = {}
        self._stats: dict[str, dict[str, Any]] = {}
        self.last_tick_ts: Optional[float] = None
        self.jobs_done = 0
        self._load_state()

    # ------------------------------------------------------------ state file
    def _load_state(self) -> None:
        if self.state_path is None:
            return
        data = atomic.read_json(self.state_path, {})
        last = data.get("last") if isinstance(data, dict) else None
        now = self.clock()
        if isinstance(last, dict):
            for kind, ts in last.items():
                if isinstance(ts, (int, float)) and not isinstance(ts, bool):
                    self.last[str(kind)] = min(float(ts), now)     # a clock that went back must not postpone a job

    def _save_state(self) -> None:
        if self.state_path is None:
            return
        try:
            atomic.write_json_atomic(self.state_path, {"last": dict(self.last)}, fsync=False)
        except OSError:
            log.warning("could not save the scheduler state to %s", self.state_path, exc_info=True)

    # ------------------------------------------------------------ registration
    def register(self, job: Job) -> Job:
        """Add (or replace) the job of that ``kind``: the template ``run_now(kind)`` and ``enqueue_due`` use. A
        job with ``every_s`` is periodic."""
        if job.lane not in self.lanes:
            raise ValueError(f"unknown lane {job.lane!r}; lanes are {sorted(self.lanes)}")
        if job.fn is None:
            raise ValueError(f"job {job.kind!r} has no fn")
        self._registered[job.kind] = job
        return job

    def _instance(self, job: Job, reason: str) -> Job:
        return dataclasses.replace(job, reason=reason)

    # ------------------------------------------------------------ lifecycle
    def _alive(self) -> bool:
        return any(t.is_alive() for t in self._threads)

    def start(self) -> None:
        """Start the lane workers and the tick thread (idempotent)."""
        if self._alive():
            return
        self._stop.clear()
        self._born = self.clock()
        self._threads = []
        for lane, workers in self.lanes.items():
            for i in range(workers):
                t = threading.Thread(target=self._worker, args=(lane,), name=f"{self.name}-{lane}-{i}", daemon=True)
                self._threads.append(t)
                t.start()
        ticker = threading.Thread(target=self._ticker, name=f"{self.name}-tick", daemon=True)
        self._threads.append(ticker)
        ticker.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Ask the threads to stop and wait up to ``timeout`` seconds for them (a long running job is not killed)."""
        self._stop.set()
        for lane, workers in self.lanes.items():
            for _ in range(workers):
                self._queues[lane].put(None)                     # wake the workers; None is ignored while running
        deadline = time.monotonic() + timeout
        for t in self._threads:
            t.join(max(0.0, deadline - time.monotonic()))
        self._save_state()

    # ------------------------------------------------------------ submitting
    def submit(self, job: Job) -> Optional[Job]:
        """Queue ``job`` on its lane. Returns the job, or ``None`` when one with the same key is already queued or
        running (so "scan now" pressed twice runs once)."""
        if job.lane not in self.lanes:
            raise ValueError(f"unknown lane {job.lane!r}; lanes are {sorted(self.lanes)}")
        if job.fn is None:
            raise ValueError(f"job {job.kind!r} has no fn")
        key = job.dedupe_key
        with self._lock:
            if key in self._pending:
                return None
            self._pending.add(key)
        self._queues[job.lane].put(job)
        return job

    def run_now(self, job: Union[str, Job], timeout: float = 240.0) -> Any:
        """Run a registered job (by ``kind``) or a given :class:`Job` and wait for its result.

        Without running lanes (tests, MCP-only mode) it runs inline in the caller's thread and exceptions propagate.
        With lanes: ``{"queued": True, "note": "already queued"}`` when the same key is pending,
        ``{"queued": True, "note": "still running; ..."}`` when ``timeout`` passed first, ``RuntimeError`` when the
        job failed, otherwise the job's return value."""
        if isinstance(job, str):
            template = self._registered.get(job)
            if template is None:
                raise ValueError(f"unknown job kind {job!r}")
            instance = self._instance(template, "manual")
        else:
            instance = job
        if not self._alive():
            return self._execute(instance)
        queued = self.submit(instance)
        if queued is None:
            return {"queued": True, "note": "already queued"}
        if not queued.done.wait(timeout):
            return {"queued": True, "note": "still running; the result will appear shortly"}
        if queued.error:
            raise RuntimeError(queued.error)
        return queued.result

    # ------------------------------------------------------------ periodic jobs
    def _every(self, job: Job) -> float:
        try:
            value = job.every_s() if callable(job.every_s) else job.every_s
            return float(value or 0.0)
        except Exception:  # noqa: BLE001 - a broken settings getter must not stop the others
            log.exception("every_s of %s failed", job.kind)
            return 0.0

    def is_due(self, job: Job, now: Optional[float] = None) -> bool:
        """Whether a periodic job should be queued at ``now``: never run and ``first_delay_s`` since the start has
        passed, or ``every_s`` since its last run."""
        now = self.clock() if now is None else now
        every = self._every(job)
        if every <= 0:
            return False
        last = self.last.get(job.kind)
        if last is None:
            return now - self._born >= job.first_delay_s
        return now - last >= every

    def enqueue_due(self, now: Optional[float] = None) -> int:
        """Queue every periodic job that is due (and whose ``enabled`` says yes); returns how many were queued.
        Does not look at ``paused`` or ``enabled`` of the scheduler: the tick does."""
        now = self.clock() if now is None else now
        n = 0
        for job in list(self._registered.values()):
            if not self.is_due(job, now):
                continue
            try:
                if job.enabled is not None and not job.enabled():
                    continue
            except Exception:  # noqa: BLE001
                log.exception("enabled() of %s failed", job.kind)
                continue
            if self.submit(self._instance(job, "schedule")) is not None:
                n += 1
        return n

    # ------------------------------------------------------------ threads
    def _ticker(self) -> None:
        while not self._stop.is_set():
            now = self.clock()
            self.last_tick_ts = now
            if self.enabled and not self.paused():
                try:
                    self.enqueue_due(now)
                except Exception:  # noqa: BLE001
                    log.exception("scheduler tick failed")
            if self._stop.wait(self.tick_s):
                break

    def _worker(self, lane: str) -> None:
        q = self._queues[lane]
        while not self._stop.is_set():
            try:
                job = q.get(timeout=0.5)
            except queue.Empty:
                continue
            if job is None:
                continue
            self._run(job, lane)

    def _run(self, job: Job, lane: str) -> None:
        with self._lock:
            self._current[lane].append(job)
        try:
            job.result = self._execute(job)
        except Exception as error:  # noqa: BLE001
            job.error = f"{type(error).__name__}: {error}"
            log.warning("job %s failed: %s", job.kind, job.error)
            if self.on_error is not None:
                try:
                    self.on_error(job, error)
                except Exception:  # noqa: BLE001
                    log.debug("on_error failed", exc_info=True)
        finally:
            with self._lock:
                self._pending.discard(job.dedupe_key)
                if job in self._current[lane]:
                    self._current[lane].remove(job)
                self.jobs_done += 1
            job.done.set()

    def _execute(self, job: Job) -> Any:
        started = self.clock()
        self.last[job.kind] = started
        stats = self._stats.setdefault(job.kind, {"runs": 0, "failures": 0, "last_error": "", "last_ms": 0})
        t0 = time.monotonic()
        try:
            result = job.fn() if job.fn is not None else None
        except BaseException as exc:
            stats["failures"] += 1
            stats["last_error"] = f"{type(exc).__name__}: {exc}"
            raise
        else:
            stats["last_error"] = ""
            return result
        finally:
            stats["runs"] += 1
            stats["last_ms"] = int((time.monotonic() - t0) * 1000)
            self._save_state()

    # ------------------------------------------------------------ view
    def status(self) -> dict[str, Any]:
        def view(job: Job) -> dict[str, str]:
            return {"kind": job.kind, "key": job.dedupe_key, "reason": job.reason}
        with self._lock:
            lanes = {lane: {"workers": self.lanes[lane], "queue": self._queues[lane].qsize(),
                            "current": [view(j) for j in self._current[lane]]} for lane in self.lanes}
        jobs = {}
        for kind, job in self._registered.items():
            jobs[kind] = {"lane": job.lane, "every_s": self._every(job) or None, "last_ts": self.last.get(kind),
                          **{k: v for k, v in self._stats.get(kind, {}).items()}}
        return {"enabled": self.enabled, "running": self._alive(), "paused": bool(self.paused()), "lanes": lanes, "jobs": jobs,
                "last_tick_ts": self.last_tick_ts, "jobs_done": self.jobs_done}


# ================================================================== JobQueue

class JobCancelled(Exception):
    """Raised inside a handler (by ``ctx.check()`` / ``ctx.progress()``) once a cancel was requested."""


class JobNotFound(KeyError):
    """No job with that id."""


class WaitingForResources(Exception):
    """Raised by a handler that cannot run *now* (not enough free VRAM, a device busy): the job goes to ``waiting``
    and is queued again after ``retry_in_s`` seconds, until ``max_waiting_s`` has passed since it first waited."""

    def __init__(self, reason: str = "waiting for resources", *, retry_in_s: float = 15.0):
        super().__init__(reason)
        self.reason = reason
        self.retry_in_s = retry_in_s


_TABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
TERMINAL = ("done", "error", "cancelled", "interrupted")
ACTIVE = ("queued", "running", "waiting")


def jobs_schema(table: str = "jobs") -> str:
    """The SQL that creates the queue's table and index; put it in an app migration if you prefer (``JobQueue``
    also runs it with ``IF NOT EXISTS`` when it starts)."""
    if not _TABLE.match(table):
        raise ValueError(f"not a table name: {table!r}")
    return f"""
CREATE TABLE IF NOT EXISTS {table} (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  lane TEXT NOT NULL DEFAULT 'work',
  state TEXT NOT NULL DEFAULT 'queued',
  label TEXT NOT NULL DEFAULT '',
  payload TEXT NOT NULL DEFAULT '{{}}',
  result TEXT NOT NULL DEFAULT '{{}}',
  error TEXT NOT NULL DEFAULT '',
  progress REAL NOT NULL DEFAULT 0,
  message TEXT NOT NULL DEFAULT '',
  dedupe_key TEXT,
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  attempts INTEGER NOT NULL DEFAULT 0,
  waiting_since REAL,
  created_ts REAL NOT NULL,
  started_ts REAL,
  finished_ts REAL
);
CREATE INDEX IF NOT EXISTS {table}_state ON {table}(state, created_ts);
"""


class JobCtx:
    """What a handler receives: the job's ``id``, ``kind``, ``payload`` and ``attempt``, ``progress`` and ``check``."""

    def __init__(self, jobs: "JobQueue", job_id: str, kind: str, payload: dict[str, Any], attempt: int):
        self._jobs = jobs
        self.id = job_id
        self.kind = kind
        self.payload = payload
        self.attempt = attempt
        self.cancelled = threading.Event()
        self.cancel_reason = ""
        self._last_write = 0.0

    def request_cancel(self, reason: str = "cancel") -> None:
        self.cancel_reason = self.cancel_reason or reason
        self.cancelled.set()

    def check(self) -> None:
        """Raise :class:`JobCancelled` when a cancel was requested (call it between steps of long work)."""
        if self.cancelled.is_set():
            raise JobCancelled(self.cancel_reason or "cancelled")

    def progress(self, pct: float, msg: Optional[str] = None, *, force: bool = False) -> None:
        """Record ``pct`` (0 to 100) and an optional message, then raise :class:`JobCancelled` if a cancel was
        requested. Writes are throttled to one every 0.4 s (completion and ``force`` always write)."""
        self.check()
        now = time.monotonic()
        value = max(0.0, min(100.0, float(pct)))
        if force or value >= 100 or now - self._last_write >= 0.4:
            self._last_write = now
            self._jobs._write_progress(self.id, value, msg)


class JobQueue:
    """Jobs persisted in a ``sqlkit.Database`` table, run by worker threads grouped in lanes.

    ``lanes`` maps lane name to worker count (default ``{"work": 2}``). Handlers are registered by ``kind`` with
    :meth:`register` and receive a :class:`JobCtx`; they return a JSON-serialisable result (``None`` gives ``{}``)
    and may raise :class:`JobCancelled` (done for them by ``ctx.check()``) or :class:`WaitingForResources`. States:
    ``queued``, ``running``, ``waiting``, ``done``, ``error``, ``cancelled``, ``interrupted``.

    ``inline=True`` runs every job synchronously inside :meth:`submit` (tests). ``on_event(name, view)`` hears
    ``queued``, ``started``, ``progress`` (not throttled by the hook), ``waiting`` and the end states; ``on_done(view)``
    hears the end states only; failures of both are logged and ignored.
    """

    def __init__(self, db: Database, lanes: Optional[Mapping[str, int]] = None, *, table: str = "jobs",
                 clock: Callable[[], float] = time.time, on_done: Optional[Callable[[dict], None]] = None,
                 on_event: Optional[Callable[[str, dict], None]] = None, inline: bool = False,
                 max_waiting_s: float = 1800.0, name: str = "jobs"):
        if not _TABLE.match(table):
            raise ValueError(f"not a table name: {table!r}")
        self.db = db
        self.table = table
        self.lanes: dict[str, int] = {str(k): max(1, int(v)) for k, v in (lanes or {"work": 2}).items()}
        self.clock = clock
        self.on_done = on_done
        self.on_event = on_event
        self.inline = inline
        self.max_waiting_s = max_waiting_s
        self.name = name
        self._handlers: dict[str, tuple[Callable[[JobCtx], Any], str, str]] = {}
        self._queues: dict[str, "queue.Queue[Optional[str]]"] = {lane: queue.Queue() for lane in self.lanes}
        self._running: dict[str, JobCtx] = {}
        self._timers: set[threading.Timer] = set()
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.db.script(jobs_schema(table))

    # ------------------------------------------------------------ registration
    def register(self, kind: str, fn: Callable[[JobCtx], Any], *, lane: str = "work", on_restart: str = "interrupted") -> None:
        """Handler for ``kind``. ``on_restart`` says what happens to a job of this kind that was running when the
        app stopped: ``"interrupted"`` (mark it, the caller decides) or ``"requeue"`` (run it again; only for
        idempotent work)."""
        if lane not in self.lanes:
            raise ValueError(f"unknown lane {lane!r}; lanes are {sorted(self.lanes)}")
        if on_restart not in ("interrupted", "requeue"):
            raise ValueError("on_restart must be 'interrupted' or 'requeue'")
        self._handlers[kind] = (fn, lane, on_restart)

    # ------------------------------------------------------------ events
    def _emit(self, name: str, job_id: str) -> None:
        if self.on_event is None and not (self.on_done and name in TERMINAL):
            return
        try:
            view = self.get(job_id)
        except JobNotFound:
            return
        for hook, args in ((self.on_event, (name, view)), (self.on_done if name in TERMINAL else None, (view,))):
            if hook is None:
                continue
            try:
                hook(*args)
            except Exception:  # noqa: BLE001 - hooks are hints
                log.debug("job hook failed", exc_info=True)

    # ------------------------------------------------------------ lifecycle
    def start(self) -> int:
        """Handle what the previous run left behind (:meth:`requeue_interrupted`), then start the lane workers.
        Returns the number of jobs that had been running."""
        self._stop.clear()
        handled = self.requeue_interrupted()
        if not self.inline:
            for lane, workers in self.lanes.items():
                for i in range(workers):
                    t = threading.Thread(target=self._worker, args=(lane,), name=f"{self.name}-{lane}-{i}", daemon=True)
                    self._threads.append(t)
                    t.start()
        for row in self.db.query(f"SELECT id, lane FROM {self.table} WHERE state = 'queued' ORDER BY created_ts, id"):
            if self.inline:
                self._run(row["id"])
            else:
                self._enqueue(row["id"], row["lane"])
        return handled

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the workers: running jobs get a cancel with reason ``shutdown`` (they end as ``interrupted``), timers
        for waiting jobs are dropped (those jobs stay ``waiting`` and are queued again by the next start)."""
        self._stop.set()
        with self._lock:
            for ctx in self._running.values():
                ctx.request_cancel("shutdown")
            timers, self._timers = list(self._timers), set()
        for timer in timers:
            timer.cancel()
        for lane, workers in self.lanes.items():
            for _ in range(workers):
                self._queues[lane].put(None)
        deadline = time.monotonic() + timeout
        for t in self._threads:
            t.join(max(0.0, deadline - time.monotonic()))
        self._threads = []

    def requeue_interrupted(self) -> int:
        """What was ``running`` when the app stopped did not finish: mark it ``interrupted`` (error text says why) or,
        for kinds registered with ``on_restart="requeue"``, queue it again; ``waiting`` jobs go back to ``queued``.
        Returns how many ``running`` jobs were touched."""
        now = self.clock()
        touched = 0
        with self.db.tx():
            for row in self.db.query(f"SELECT id, kind FROM {self.table} WHERE state = 'running'"):
                handler = self._handlers.get(row["kind"])
                if handler is not None and handler[2] == "requeue":
                    self.db.execute(f"UPDATE {self.table} SET state = 'queued', progress = 0, message = '', started_ts = NULL WHERE id = ?", (row["id"],))
                else:
                    self.db.execute(f"UPDATE {self.table} SET state = 'interrupted', error = ?, finished_ts = ? WHERE id = ?",
                                    ("Interrupted: the app stopped while it ran.", now, row["id"]))
                touched += 1
            self.db.execute(f"UPDATE {self.table} SET state = 'queued' WHERE state = 'waiting'")
        return touched

    # ------------------------------------------------------------ submit / cancel
    def submit(self, kind: str, payload: Optional[Mapping[str, Any]] = None, lane: Optional[str] = None, *, label: str = "",
               dedupe_key: Optional[str] = None) -> str:
        """Persist a job and queue it; returns its id (ULID based, ordered by creation). With ``dedupe_key``, an
        active job of the same kind and key is returned instead of making a second one."""
        handler = self._handlers.get(kind)
        if handler is None:
            raise ValueError(f"unknown job kind {kind!r}")
        lane = lane or handler[1]
        if lane not in self.lanes:
            raise ValueError(f"unknown lane {lane!r}; lanes are {sorted(self.lanes)}")
        with self.db.tx():
            if dedupe_key is not None:
                row = self.db.one(f"SELECT id FROM {self.table} WHERE kind = ? AND dedupe_key = ? AND state IN ('queued','running','waiting')",
                                  (kind, dedupe_key))
                if row is not None:
                    return row["id"]
            job_id = new_id("job")
            self.db.execute(f"INSERT INTO {self.table}(id, kind, lane, state, label, payload, dedupe_key, created_ts) VALUES (?, ?, ?, 'queued', ?, ?, ?, ?)",
                            (job_id, kind, lane, label[:200], dumps(dict(payload or {})), dedupe_key, self.clock()))
        self._emit("queued", job_id)
        if self.inline:
            self._run(job_id)
        else:
            self._enqueue(job_id, lane)
        return job_id

    def cancel(self, job_id: str) -> dict[str, Any]:
        """Cooperative cancel. A queued job is cancelled at once; a running one gets a flag that ``ctx.check()`` /
        ``ctx.progress()`` turn into :class:`JobCancelled`; a finished one is left alone. Returns the job."""
        job = self.get(job_id)
        if job["state"] in ("queued", "waiting"):
            changed = self.db.execute(f"UPDATE {self.table} SET state = 'cancelled', cancel_requested = 1, error = 'Cancelled.', finished_ts = ? "
                                      "WHERE id = ? AND state IN ('queued','waiting')", (self.clock(), job_id)).rowcount
            if changed:
                self._emit("cancelled", job_id)
        elif job["state"] == "running":
            self.db.execute(f"UPDATE {self.table} SET cancel_requested = 1 WHERE id = ?", (job_id,))
            with self._lock:
                ctx = self._running.get(job_id)
            if ctx is not None:
                ctx.request_cancel("cancel")
        return self.get(job_id)

    # ------------------------------------------------------------ read
    def _view(self, row: Any) -> dict[str, Any]:
        started, finished = row["started_ts"], row["finished_ts"]
        return {
            "id": row["id"], "kind": row["kind"], "lane": row["lane"], "state": row["state"], "label": row["label"],
            "progress": round(row["progress"], 2), "message": row["message"], "payload": loads(row["payload"], {}),
            "result": loads(row["result"], {}), "error": row["error"], "attempts": row["attempts"],
            "cancel_requested": bool(row["cancel_requested"]), "dedupe_key": row["dedupe_key"],
            "created_ts": row["created_ts"], "started_ts": started, "finished_ts": finished,
            "elapsed_s": round((finished or self.clock()) - started, 1) if started else None,
        }

    def get(self, job_id: str) -> dict[str, Any]:
        row = self.db.one(f"SELECT * FROM {self.table} WHERE id = ?", (job_id,))
        if row is None:
            raise JobNotFound(job_id)
        return self._view(row)

    def list(self, *, state: Optional[str] = None, kind: Optional[str] = None, limit: int = 50) -> list[dict[str, Any]]:
        """Newest first. ``state="active"`` means queued, running or waiting."""
        where, args = [], []
        if state == "active":
            where.append("state IN ('queued','running','waiting')")
        elif state:
            where.append("state = ?")
            args.append(state)
        if kind:
            where.append("kind = ?")
            args.append(kind)
        sql = f"SELECT * FROM {self.table}" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY created_ts DESC, id DESC LIMIT ?"
        return [self._view(r) for r in self.db.query(sql, (*args, int(limit)))]

    def wait(self, job_id: str, wait_s: float = MAX_WAIT_S, *, poll: float = 0.1) -> dict[str, Any]:
        """:func:`hoard_link.waiting.wait_for` on this job (the wait is clamped to ``MAX_WAIT_S``)."""
        return wait_for(lambda: self.get(job_id), wait_s, poll=poll)

    def purge_finished(self, older_than_s: float = 7 * 86400.0) -> int:
        """Delete finished jobs older than ``older_than_s``; returns how many."""
        cutoff = self.clock() - older_than_s
        return self.db.execute(f"DELETE FROM {self.table} WHERE state IN ('done','error','cancelled','interrupted') AND finished_ts < ?", (cutoff,)).rowcount

    # ------------------------------------------------------------ workers
    def _enqueue(self, job_id: str, lane: str) -> None:
        self._queues[lane if lane in self._queues else next(iter(self._queues))].put(job_id)

    def _worker(self, lane: str) -> None:
        q = self._queues[lane]
        while not self._stop.is_set():
            job_id = q.get()
            if job_id is None or self._stop.is_set():
                continue
            try:
                self._run(job_id)
            except Exception:  # noqa: BLE001
                log.exception("job loop")

    def _write_progress(self, job_id: str, pct: float, msg: Optional[str]) -> None:
        if msg is None:
            self.db.execute(f"UPDATE {self.table} SET progress = ? WHERE id = ?", (pct, job_id))
        else:
            self.db.execute(f"UPDATE {self.table} SET progress = ?, message = ? WHERE id = ?", (pct, str(msg)[:300], job_id))
        if self.on_event is not None:
            try:
                self.on_event("progress", self.get(job_id))
            except Exception:  # noqa: BLE001
                log.debug("progress hook failed", exc_info=True)

    def _finish(self, job_id: str, state: str, result: Any = None, error: str = "") -> None:
        self.db.execute(
            f"UPDATE {self.table} SET state = ?, result = ?, error = ?, finished_ts = ?, "
            f"progress = CASE WHEN ? = 'done' THEN 100 ELSE progress END WHERE id = ?",
            (state, dumps(result if result is not None else {}), error[:2000], self.clock(), state, job_id))
        self._emit(state, job_id)

    def _run(self, job_id: str) -> None:
        with self.db.tx():
            row = self.db.one(f"SELECT * FROM {self.table} WHERE id = ?", (job_id,))
            if row is None or row["state"] != "queued":
                return                                  # cancelled while queued, or already claimed
            self.db.execute(f"UPDATE {self.table} SET state = 'running', started_ts = ?, attempts = attempts + 1, progress = 0, message = '' WHERE id = ?",
                            (self.clock(), job_id))
        handler = self._handlers.get(row["kind"])
        self._emit("started", job_id)
        if handler is None:
            self._finish(job_id, "error", error=f"No handler registered for job kind {row['kind']!r}.")
            return
        ctx = JobCtx(self, job_id, row["kind"], loads(row["payload"], {}), int(row["attempts"]) + 1)
        with self._lock:
            self._running[job_id] = ctx
            if self._stop.is_set():
                ctx.request_cancel("shutdown")
        try:
            result = handler[0](ctx)
        except JobCancelled:
            self._finish(job_id, "interrupted" if ctx.cancel_reason == "shutdown" else "cancelled",
                         error="Interrupted: the app is stopping." if ctx.cancel_reason == "shutdown" else "Cancelled.")
        except WaitingForResources as wait:
            self._waiting(job_id, row, wait)
        except Exception as exc:  # noqa: BLE001
            log.error("job %s (%s) failed:\n%s", job_id, row["kind"], traceback.format_exc())
            self._finish(job_id, "error", error=f"{type(exc).__name__}: {exc}")
        else:
            self._finish(job_id, "done", result=result)
        finally:
            with self._lock:
                self._running.pop(job_id, None)

    def _waiting(self, job_id: str, row: Any, wait: WaitingForResources) -> None:
        now = self.clock()
        since = row["waiting_since"] or now
        if now - since > self.max_waiting_s:
            self._finish(job_id, "error", error=f"Gave up waiting for resources: {wait.reason}")
            return
        self.db.execute(f"UPDATE {self.table} SET state = 'waiting', message = ?, waiting_since = ? WHERE id = ?",
                        (wait.reason[:300], since, job_id))
        self._emit("waiting", job_id)
        if self._stop.is_set():
            return

        def requeue() -> None:
            with self._lock:
                self._timers.discard(timer)
            changed = self.db.execute(f"UPDATE {self.table} SET state = 'queued' WHERE id = ? AND state = 'waiting'", (job_id,)).rowcount
            if changed and not self._stop.is_set():
                if self.inline:
                    self._run(job_id)
                else:
                    self._enqueue(job_id, row["lane"])

        timer = threading.Timer(max(0.0, wait.retry_in_s), requeue)
        timer.daemon = True
        with self._lock:
            self._timers.add(timer)
        timer.start()
