"""Detached agent-run manager.

Keeps an agent/chat stream running server-side after the SSE client disconnects
(tab close, navigate away, refresh). The streaming generator is drained by a
background asyncio task into a per-session replay buffer; SSE clients SUBSCRIBE
to that buffer (replay everything so far, then live). Closing the SSE only drops
the subscriber — the drain task keeps going.

The wrapped generator already persists the assistant message to the session on
completion, so reopening the session shows the finished result even if nobody
was connected when it finished. Reconnecting mid-run replays the buffer + streams
live (pick up where it is).

Durability
----------
* In memory while the server process runs (tab close / navigation / refresh).
* On disk as a replay log (DATA_DIR/runs/<session>.jsonl) so a run that the
  process took down with it (restart, crash) is not lost: at the next startup
  `recover_interrupted_runs()` turns every log that never reached a terminal
  status into a saved, clearly-marked partial assistant message ("interrupted
  by a restart") and flags the chat in the sidebar. The generation itself
  cannot be resumed — the model state is gone — but nothing the run produced
  disappears, and the user can press Continue.

Queue
-----
Runs may carry a *lane* ("local" for a local GPU endpoint). A lane admits
`limit` concurrent runs (setting `agent_queue_local_concurrency`, default 1 —
one GPU, one generation); the rest wait FIFO with a live `queue_status`
event (position) so several requests can be fired off and the GPU works
through them one by one, each chat notifying when it is done. Stop works on a
queued run too (it leaves the queue).
"""
import asyncio
import json
import logging
import os
import re
import threading
import time
import uuid
from typing import Any, AsyncGenerator, Dict, List, Optional

logger = logging.getLogger(__name__)


def _setting(key: str, default: Any) -> Any:
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:
        return default


class _Run:
    __slots__ = ("buffer", "subscribers", "status", "task", "evict_task", "run_id", "last_key",
                 "lane", "queued_position", "log", "started_at", "label")

    def __init__(self, lane: Optional[str] = None, label: str = "") -> None:
        self.buffer: list = []          # ordered SSE event strings (replay log)
        self.subscribers: set = set()   # one asyncio.Queue per connected client
        self.status: str = "running"    # running | done | error | stopped
        self.task: Optional[asyncio.Task] = None
        self.evict_task: Optional[asyncio.Task] = None
        # Stable across every subscription/replay of this exact detached run.
        # The browser uses it to make local cost accounting replay-idempotent.
        self.run_id: str = uuid.uuid4().hex
        self.last_key: Optional[str] = None   # compaction key of buffer[-1] (see _compact_key)
        self.lane: Optional[str] = lane
        self.queued_position: int = 0         # >0 while waiting for the lane
        self.log: Optional["_RunLog"] = None
        self.started_at: float = time.time()
        self.label: str = label


_RUNS: Dict[str, _Run] = {}

# How long a FINISHED run (and its full replay buffer) is retained after the
# last subscriber disconnects, so a reconnect within the window can still
# replay the result. After this, the run is evicted to bound memory — without
# it, every session that ever streamed kept its entire event log forever.
_EVICT_GRACE_S = 180


_PROGRESS_PREFIX = 'data: {"type": "tool_progress"'


def _compact_key(ev: str) -> Optional[str]:
    """Replay-log compaction key for a live-progress event, else None.

    A long bash/python command emits a `tool_progress` (elapsed + stdout tail)
    every 2 s; only the LATEST one matters for a client that reconnects, and a
    1-hour command would otherwise leave ~1800 near-identical events in the
    buffer. Consecutive progress events of the same tool call collapse into
    one slot. Sub-agent board events are never merged (each is a state change).
    """
    if not ev.startswith(_PROGRESS_PREFIX) or '"subagent"' in ev:
        return None
    try:
        d = json.loads(ev[6:])
    except Exception:
        return None
    return f"{d.get('tool')}|{d.get('round')}|{d.get('approved')}"


# ---------------------------------------------------------------------------
# On-disk replay log
# ---------------------------------------------------------------------------

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]")


def _runs_dir() -> str:
    try:
        from src.constants import DATA_DIR
    except Exception:  # pragma: no cover
        DATA_DIR = os.path.join(os.getcwd(), "data")
    return os.path.join(DATA_DIR, "runs")


def _log_path(session_id: str) -> str:
    return os.path.join(_runs_dir(), _SAFE_NAME_RE.sub("_", str(session_id))[:120] + ".jsonl")


def persistence_enabled() -> bool:
    return bool(_setting("agent_runs_persist", True))


class _RunLog:
    """Append-only JSONL mirror of a run's replay buffer. Deltas are flushed in
    small batches; every other event (tool cards, harness, status) is flushed
    immediately so a crash loses at most a few tokens of prose."""

    def __init__(self, session_id: str, run: _Run):
        self.path = _log_path(session_id)
        self.session_id = session_id
        self._f = None
        self._pending = 0
        self._orphaned = False
        self._lock = threading.Lock()
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            self._f = open(self.path, "w", encoding="utf-8")
            self._write({"status": "running", "run_id": run.run_id, "ts": time.time(),
                         "session_id": session_id, "lane": run.lane, "label": run.label}, flush=True)
        except OSError as e:
            logger.debug("[agent-run] log unavailable for %s: %s", session_id, e)
            self._f = None

    def orphan(self) -> None:
        """Detach this log from its file: the run it belongs to was replaced and
        a NEW _RunLog now owns `self.path` (same session → same file name, and
        it opened the file with "w"). Anything this one wrote afterwards — its
        remaining events, and above all its `finish()` status line — landed
        INSIDE the new run's log, which then read back as terminal (so
        `recover_interrupted_runs` skipped the live run) or as a mix of both
        runs' text. Every later write is a no-op and the descriptor is closed.
        """
        with self._lock:
            self._orphaned = True
            try:
                if self._f is not None:
                    self._f.close()
            except OSError:
                pass
            self._f = None

    def _write(self, obj: dict, flush: bool) -> None:
        if self._f is None or self._orphaned:
            return
        with self._lock:
            # Re-check inside the lock: orphan() may have closed the file
            # between the fast path above and here.
            if self._f is None or self._orphaned:
                return
            try:
                self._f.write(json.dumps(obj, ensure_ascii=False) + "\n")
                self._pending += 1
                if flush or self._pending >= 25:
                    self._f.flush()
                    self._pending = 0
            except (OSError, ValueError):
                self._f = None

    def event(self, seq: int, ev: str, replaced: bool) -> None:
        is_delta = ev.startswith('data: {"delta"')
        self._write({"seq": seq, "ev": ev, "r": replaced} if replaced else {"seq": seq, "ev": ev}, flush=not is_delta)

    def finish(self, status: str) -> None:
        self._write({"status": status, "ts": time.time()}, flush=True)
        with self._lock:
            try:
                if self._f is not None:
                    self._f.close()
            except OSError:
                pass
            self._f = None


def _publish(run: _Run, ev: str) -> None:
    """Append one SSE event (or replace the previous progress tick of the same
    tool call) and fan it out to every live subscriber."""
    key = _compact_key(ev)
    replaced = False
    if key is not None and run.last_key == key and run.buffer:
        run.buffer[-1] = ev
        replaced = True
    else:
        run.buffer.append(ev)
    run.last_key = key
    seq = len(run.buffer) - 1
    if run.log is not None:
        run.log.event(seq, ev, replaced)
    for q in list(run.subscribers):
        try:
            q.put_nowait((seq, ev, replaced))
        except Exception:
            pass


def _wake_run_subscribers(run: _Run) -> None:
    """Close subscribers even when the drain task never reached its body."""
    for q in list(run.subscribers):
        try:
            q.put_nowait((None, None, False))
        except Exception:
            pass


def _schedule_evict(session_id: str, expected_run: Optional[_Run] = None) -> None:
    """(Re)arm a grace-period eviction for a terminal run with no subscribers.
    Identity-checked so a run that gets replaced/reused is never evicted by a
    stale timer."""
    run = _RUNS.get(session_id)
    if run is None:
        return
    if expected_run is not None and run is not expected_run:
        return
    if run.evict_task and not run.evict_task.done():
        run.evict_task.cancel()

    async def _evict(run_ref: _Run) -> None:
        try:
            await asyncio.sleep(_EVICT_GRACE_S)
        except asyncio.CancelledError:
            return
        cur = _RUNS.get(session_id)
        if cur is run_ref and cur.status != "running" and not cur.subscribers:
            _RUNS.pop(session_id, None)

    run.evict_task = asyncio.create_task(_evict(run))


def is_active(session_id: str) -> bool:
    r = _RUNS.get(session_id)
    return bool(r and r.status == "running")


# Sessions that are busy WITHOUT a detached run of their own — e.g. the worker
# chats of `delegate_agents`, which are driven by the parent's tool call. They
# get the same blinking dot in the sidebar while their run is in flight.
_EXTERNAL_BUSY: set = set()


def mark_busy(session_id: Optional[str]) -> None:
    if session_id:
        _EXTERNAL_BUSY.add(session_id)


def clear_busy(session_id: Optional[str]) -> None:
    if session_id:
        _EXTERNAL_BUSY.discard(session_id)


def active_session_ids() -> List[str]:
    """Sessions with a run still going (sidebar activity dots): detached runs
    plus externally-marked busy sessions (sub-agent workers)."""
    ids = [sid for sid, r in list(_RUNS.items()) if r.status == "running"]
    for sid in list(_EXTERNAL_BUSY):
        if sid not in ids:
            ids.append(sid)
    return ids


def queued_positions() -> Dict[str, int]:
    """session → 1-based queue position, for runs still waiting for their lane."""
    return {sid: r.queued_position for sid, r in list(_RUNS.items()) if r.status == "running" and r.queued_position > 0}


def get_status(session_id: str) -> Optional[str]:
    r = _RUNS.get(session_id)
    return r.status if r else None


def get_run_id(session_id: str) -> Optional[str]:
    """Return the opaque identity of the current detached run, if present."""
    r = _RUNS.get(session_id)
    return r.run_id if r else None


def get_active_run(session_id: str) -> Optional[_Run]:
    """Return the exact active run currently registered for a session."""
    r = _RUNS.get(session_id)
    return r if r and r.status == "running" else None


# ---------------------------------------------------------------------------
# Lanes (the task queue)
# ---------------------------------------------------------------------------

class _Lane:
    def __init__(self, name: str):
        self.name = name
        self.active: int = 0
        self.waiting: List[_Run] = []
        self.cond: Optional[asyncio.Condition] = None

    def _condition(self) -> asyncio.Condition:
        if self.cond is None:
            self.cond = asyncio.Condition()
        return self.cond

    @property
    def limit(self) -> int:
        key = "agent_queue_local_concurrency" if self.name == "local" else f"agent_queue_{self.name}_concurrency"
        try:
            v = int(_setting(key, 1 if self.name == "local" else 0) or 0)
        except (TypeError, ValueError):
            v = 1 if self.name == "local" else 0
        return v  # 0 = unlimited

    def positions(self) -> Dict[str, int]:
        return {r.run_id: i + 1 for i, r in enumerate(self.waiting)}

    async def acquire(self, run: _Run) -> None:
        limit = self.limit
        if limit <= 0:
            return
        cond = self._condition()
        self.waiting.append(run)
        try:
            async with cond:
                while True:
                    if self.active < limit and self.waiting and self.waiting[0] is run:
                        self.waiting.pop(0)
                        self.active += 1
                        run.queued_position = 0
                        _publish(run, "data: " + json.dumps({"type": "queue_status", "queued": False, "position": 0, "lane": self.name}) + "\n\n")
                        self._broadcast_positions()
                        return
                    pos = self.waiting.index(run) + 1 if run in self.waiting else 0
                    if pos != run.queued_position:
                        run.queued_position = pos
                        ahead = [r.label for r in self.waiting[: pos - 1]] if pos > 1 else []
                        _publish(run, "data: " + json.dumps({
                            "type": "queue_status", "queued": True, "position": pos, "lane": self.name,
                            "active": self.active, "ahead": ahead[:5],
                        }) + "\n\n")
                    await cond.wait()
        except BaseException:
            if run in self.waiting:
                self.waiting.remove(run)
            run.queued_position = 0
            async with cond:
                cond.notify_all()
            raise

    def _broadcast_positions(self) -> None:
        for i, r in enumerate(self.waiting):
            pos = i + 1
            if r.queued_position != pos:
                r.queued_position = pos
                _publish(r, "data: " + json.dumps({"type": "queue_status", "queued": True, "position": pos,
                                                   "lane": self.name, "active": self.active}) + "\n\n")

    async def release(self, run: _Run) -> None:
        if self.limit <= 0 and self.active == 0:
            return
        cond = self._condition()
        async with cond:
            self.active = max(0, self.active - 1)
            cond.notify_all()


_LANES: Dict[str, _Lane] = {}


def _lane(name: Optional[str]) -> Optional[_Lane]:
    if not name:
        return None
    lane = _LANES.get(name)
    if lane is None:
        lane = _Lane(name)
        _LANES[name] = lane
    return lane


def queue_snapshot() -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for name, lane in _LANES.items():
        out[name] = {"active": lane.active, "limit": lane.limit,
                     "waiting": [{"run_id": r.run_id, "label": r.label, "position": i + 1} for i, r in enumerate(lane.waiting)]}
    return out


async def _drain(session_id: str, run: _Run, agen: AsyncGenerator[str, None],
                 prev_task: Optional[asyncio.Task] = None) -> None:
    """Pull every event from the wrapped generator into the run buffer, fanning
    each out to live subscribers. Runs to completion regardless of subscribers."""
    subscribers_woken = False
    lane = _lane(run.lane)
    acquired = False

    def _wake_subscribers() -> None:
        nonlocal subscribers_woken
        if subscribers_woken:
            return
        subscribers_woken = True
        _wake_run_subscribers(run)

    # If this run replaced an in-flight one (rapid double-send), wait for that
    # one to fully finish first. Its CancelledError handler calls aclose(), which
    # persists its partial response — letting it complete before we start writing
    # keeps the two runs' session saves sequential instead of interleaved.
    try:
        if prev_task is not None and not prev_task.done():
            await asyncio.wait({prev_task})
        if lane is not None:
            await lane.acquire(run)
            acquired = lane.limit > 0
        async for ev in agen:
            _publish(run, ev)
        if run.status == "running":
            run.status = "done"
    except asyncio.CancelledError:
        run.status = "stopped"
        # Let the wrapped generator's own CancelledError handler run (it saves
        # the partial response to the session).
        try:
            await agen.aclose()
        except Exception:
            pass
        # A rapid third replacement can cancel this task while it is still
        # waiting for its predecessor. Close this run's subscribers promptly,
        # but keep the task alive until the predecessor finishes so the next
        # run still observes the transitive session-save ordering barrier.
        _wake_subscribers()
        if prev_task is not None and not prev_task.done():
            try:
                await asyncio.shield(prev_task)
            except (asyncio.CancelledError, Exception):
                pass
    except Exception as e:
        logger.error("[agent-run] %s failed: %s", session_id, e, exc_info=True)
        run.status = "error"
        _publish(
            run,
            "event: error\n"
            f"data: {json.dumps({'error': 'Agent run failed before completion.', 'status': 500})}\n\n",
        )
        _publish(run, "data: [DONE]\n\n")
    finally:
        if lane is not None and acquired:
            try:
                await lane.release(run)
            except Exception:
                pass
        if run.log is not None:
            try:
                run.log.finish(run.status if run.status != "running" else "done")
            except Exception:
                pass
        # Wake every subscriber with the end sentinel so their SSE closes.
        _wake_subscribers()
        # Run is terminal — arm the grace timer so it (and its buffer) is
        # eventually freed even if nobody ever reconnects. subscribe() cancels
        # this on connect and re-arms on disconnect.
        _schedule_evict(session_id, run)


def start(session_id: str, agen: AsyncGenerator[str, None], lane: Optional[str] = None, label: str = "") -> _Run:
    """Start a detached run draining `agen` for a session. If a run is already in
    flight for this session (e.g. a rapid double-send), it's cancelled first.

    `lane` puts the run in a FIFO queue shared by every run of that lane
    (see _Lane); None runs immediately. `label` is what other queued chats see
    as "ahead of you"."""
    prev = _RUNS.get(session_id)
    prev_task: Optional[asyncio.Task] = None
    if prev:
        if prev.task and not prev.task.done():
            # A task cancelled before its first instruction never enters
            # _drain(), so its except/finally blocks cannot update status or
            # wake a response already bound to this exact run. Terminalize it
            # synchronously before cancelling; _drain's cleanup is idempotent
            # when the task had already started.
            if prev.status == "running":
                prev.status = "stopped"
                _wake_run_subscribers(prev)
            prev.task.cancel()
            prev_task = prev.task   # new run awaits this before it starts writing
        if prev.evict_task and not prev.evict_task.done():
            prev.evict_task.cancel()
        # The replay log is named after the SESSION, so the _RunLog built below
        # truncates the very file `prev` still has open. Retire the old one
        # first: from here on its writes (including the finish() its cancelled
        # _drain is about to emit) must not reach the new run's log.
        if prev.log is not None:
            try:
                prev.log.orphan()
            except Exception as e:      # pragma: no cover - best effort
                logger.debug("[agent-run] could not orphan the previous log: %s", e)
    run = _Run(lane=lane, label=label)
    _RUNS[session_id] = run
    if persistence_enabled():
        try:
            run.log = _RunLog(session_id, run)
        except Exception as e:
            logger.debug("[agent-run] log init failed: %s", e)
            run.log = None
    run.task = asyncio.create_task(_drain(session_id, run, agen, prev_task))
    return run


async def subscribe(
    session_id: str,
    expected_run: Optional[_Run] = None,
) -> AsyncGenerator[str, None]:
    """Replay the run's buffer from the start, then stream live until it ends.
    Safe to call repeatedly (reconnect) and from multiple clients at once.

    ``expected_run`` binds a lazy StreamingResponse body to the same run whose
    identity was put in its response headers. Without that binding, a rapid
    replacement between response construction and body iteration could replay
    the replacement run under the prior run's identity.
    """
    run = expected_run or _RUNS.get(session_id)
    if run is None:
        return
    q: asyncio.Queue = asyncio.Queue()
    run.subscribers.add(q)            # register BEFORE replaying so nothing is missed
    # A live subscriber is connected — don't let a pending grace timer evict
    # the run out from under it mid-replay.
    if run.evict_task and not run.evict_task.done():
        run.evict_task.cancel()
    try:
        next_seq = 0
        while next_seq < len(run.buffer):
            yield run.buffer[next_seq]
            next_seq += 1
        if run.status != "running":
            return
        heartbeat_idx = 0
        while True:
            try:
                seq, ev, replaced = await asyncio.wait_for(q.get(), timeout=10.0)
            except asyncio.TimeoutError:
                # Keep slow local models/proxies alive while they prefill before
                # the first token. SSE comments are ignored by the UI but reset
                # browser/proxy idle timers, which prevents "empty response"
                # disconnects on llama.cpp first-token latencies of 30s+.
                if run.status == "running":
                    heartbeat_idx += 1
                    yield f": heartbeat {heartbeat_idx}\n\n"
                    continue
                seq, ev, replaced = (None, None, False)
            if seq is None:            # end sentinel
                while next_seq < len(run.buffer):   # flush any tail the sentinel raced
                    yield run.buffer[next_seq]
                    next_seq += 1
                break
            # Skip events already replayed from the buffer — except a compacted
            # progress tick, which reuses the slot of the tick it replaced and
            # must still reach a live client.
            if seq >= next_seq or replaced:
                yield ev
                next_seq = seq + 1
    finally:
        run.subscribers.discard(q)
        # Last subscriber gone on a finished run — (re)arm eviction so the
        # buffer doesn't linger indefinitely.
        if not run.subscribers and run.status != "running":
            _schedule_evict(session_id, run)


def stop(session_id: str, expected_run_id: Optional[str] = None) -> bool:
    """Cancel the matching in-flight run (which saves its partial output).

    A stale browser may issue Stop after another tab has replaced the session's
    run. Once the caller knows its opaque run identity, fail closed rather than
    cancelling that newer run.
    """
    run = _RUNS.get(session_id)
    if not expected_run_id or run is None or run.run_id != expected_run_id:
        return False
    if run and run.task and not run.task.done():
        run.task.cancel()
        return True
    return False


def _cancel_anywhere(task: "asyncio.Task") -> bool:
    """Cancel `task` from whatever thread we are on.

    FastAPI runs `def` routes in a threadpool, and asyncio tasks are not
    thread-safe: a bare `task.cancel()` from there can be lost. Hop through the
    task's own loop when we are not already on it.
    """
    try:
        loop = task.get_loop()
    except Exception:                                     # pragma: no cover
        loop = None
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if loop is not None and running is not loop:
        try:
            loop.call_soon_threadsafe(task.cancel)
            return True
        except RuntimeError:                              # loop already closed
            return False
    task.cancel()
    return True


def stop_for_session(session_id: str, reason: str = "session_deleted") -> bool:
    """Stop whatever run `session_id` currently has, without knowing its run_id.

    `stop()` stays fail-closed on purpose: a stale browser tab must never cancel
    the run that replaced its own. A server-side caller that is DESTROYING the
    session has no such ambiguity — there is no newer run left to protect — so
    it gets this explicit entry point instead of a relaxed `stop()`.

    Deleting a chat used to leave its run executing tools and writing files: it
    kept its queue-lane slot (with `agent_queue_local_concurrency=1` that blocks
    every other chat) and was unreachable from the UI, because /api/chat/activity
    and /api/chat/stop 404 once the session is gone.

    Returns True when there was something to stop. Safe to call off the event
    loop.
    """
    was_busy = session_id in _EXTERNAL_BUSY      # a sub-agent worker chat
    run = _RUNS.pop(session_id, None)
    clear_busy(session_id)
    _INTERRUPTED.pop(session_id, None)
    if run is None:
        return was_busy
    was_running = run.status == "running"
    if was_running:
        run.status = "stopped"
    # Close the replay log with a terminal status: the session is gone, so a
    # restart must not "recover" it into a chat that no longer exists.
    if run.log is not None:
        try:
            run.log.finish("stopped")
        except Exception:                                 # pragma: no cover
            pass
    if run.task is not None and not run.task.done():
        _cancel_anywhere(run.task)
    if run.evict_task is not None and not run.evict_task.done():
        _cancel_anywhere(run.evict_task)
    logger.info("[agent-run] run of session %s stopped (%s)", session_id, reason)
    return True


# ---------------------------------------------------------------------------
# Recovery after a restart
# ---------------------------------------------------------------------------

_INTERRUPTED: Dict[str, Dict[str, Any]] = {}
INTERRUPTED_NOTE = "[Interrupted: Faustus was restarted while this task was running. What it had produced is kept above; send \"continue\" to pick it up.]"


def _read_log(path: str) -> Dict[str, Any]:
    """Parse a run log: {"status", "run_id", "events": [ev...], "ts", "label"}."""
    events: Dict[int, str] = {}
    status = None
    meta: Dict[str, Any] = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if "status" in obj:
                    status = obj["status"]
                    if obj["status"] == "running":
                        meta = obj
                    continue
                seq = obj.get("seq")
                if isinstance(seq, int) and isinstance(obj.get("ev"), str):
                    events[seq] = obj["ev"]
    except OSError:
        return {"status": "unreadable", "events": []}
    ordered = [events[k] for k in sorted(events)]
    return {"status": status, "events": ordered, **{k: meta.get(k) for k in ("run_id", "ts", "lane", "label", "session_id")}}


def _partial_from_events(events: List[str]) -> Dict[str, Any]:
    text_parts: List[str] = []
    tool_events: List[Dict[str, Any]] = []
    metrics = None
    saved = False
    for ev in events:
        if not ev.startswith("data: ") or ev.startswith("data: [DONE]"):
            continue
        try:
            d = json.loads(ev[6:])
        except ValueError:
            continue
        if "delta" in d and not d.get("type"):
            if not d.get("thinking"):
                text_parts.append(str(d["delta"]))
        elif d.get("type") == "tool_output":
            tool_events.append({"tool": d.get("tool"), "command": str(d.get("command") or "")[:400],
                                "output": str(d.get("output") or "")[:1500], "exit_code": d.get("exit_code")})
        elif d.get("type") == "metrics":
            metrics = d.get("data")
        elif d.get("type") == "message_saved":
            saved = True
    return {"text": "".join(text_parts), "tool_events": tool_events[:60], "metrics": metrics, "saved": saved}


def recover_interrupted_runs(session_manager=None) -> List[Dict[str, Any]]:
    """Scan DATA_DIR/runs for logs left in 'running' state by a previous
    process. For each: save what the run had produced as a partial assistant
    message (unless the run had already saved one), mark the log
    'interrupted', and remember the session for the sidebar/toast. Also prunes
    finished logs older than `agent_runs_keep_hours` (default 48)."""
    d = _runs_dir()
    if not os.path.isdir(d):
        return []
    try:
        keep_h = float(_setting("agent_runs_keep_hours", 48) or 48)
    except (TypeError, ValueError):
        keep_h = 48.0
    now = time.time()
    recovered: List[Dict[str, Any]] = []
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return []
    for name in names:
        if not name.endswith(".jsonl"):
            continue
        path = os.path.join(d, name)
        info = _read_log(path)
        status = info.get("status")
        if status in ("done", "stopped", "error", "interrupted", "unreadable", None) and status != "running":
            try:
                if now - os.path.getmtime(path) > keep_h * 3600:
                    os.remove(path)
            except OSError:
                pass
            continue
        sid = str(info.get("session_id") or name[:-6])
        partial = _partial_from_events(info.get("events") or [])
        entry = {"session_id": sid, "run_id": info.get("run_id"), "ts": info.get("ts"),
                 "label": info.get("label") or "", "chars": len(partial["text"]),
                 "tool_calls": len(partial["tool_events"]), "saved_message": False}
        if session_manager is not None and not partial["saved"]:
            try:
                sess = session_manager.get_session(sid)
            except Exception:
                sess = None
            if sess is not None:
                try:
                    from core.models import ChatMessage
                    body = partial["text"].strip()
                    content = (body + "\n\n" if body else "") + INTERRUPTED_NOTE
                    meta: Dict[str, Any] = {"stopped": True, "interrupted": True, "run_id": info.get("run_id")}
                    if partial["tool_events"]:
                        meta["tool_events"] = partial["tool_events"]
                    if isinstance(partial.get("metrics"), dict):
                        meta.update({k: v for k, v in partial["metrics"].items() if k in ("model", "harness")})
                    sess.add_message(ChatMessage("assistant", content, metadata=meta))
                    session_manager.save_sessions()
                    entry["saved_message"] = True
                except Exception as e:
                    logger.warning("[agent-run] could not save interrupted run for %s: %s", sid, e)
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"status": "interrupted", "ts": now}) + "\n")
        except OSError:
            pass
        _INTERRUPTED[sid] = entry
        recovered.append(entry)
    if recovered:
        logger.warning("[agent-run] %d run(s) were interrupted by the previous restart: %s",
                       len(recovered), ", ".join(r["session_id"] for r in recovered))
    return recovered


def interrupted_runs() -> List[Dict[str, Any]]:
    return list(_INTERRUPTED.values())


def acknowledge_interrupted(session_id: Optional[str] = None) -> int:
    if session_id is None:
        n = len(_INTERRUPTED)
        _INTERRUPTED.clear()
        return n
    return 1 if _INTERRUPTED.pop(session_id, None) is not None else 0
