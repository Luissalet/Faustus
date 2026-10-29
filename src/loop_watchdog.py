"""Event-loop stall watchdog.

During long agent turns the server's event loop has been seen not answering for
15-45 s (`/api/health` and `/api/chat/activity` time out while the process and
the listener stay alive). Whatever blocks it is synchronous work running inside
a coroutine, and it leaves no trace in the logs. This module records it:

* a task on the loop stamps a heartbeat every `TICK_S`;
* a daemon thread, which the blocked loop cannot stop, notices a heartbeat older
  than `STALL_S` and captures the stack of the loop's thread. The innermost
  frames of that stack are the blocking call. A long stall is captured again
  every `REDUMP_S`, so a stall that moves from one call to another shows both;
* when the loop recovers, the stall is written to `<data>/logs/loop_stalls.jsonl`
  with its duration, the stacks and what the other threads were doing.

It never blocks the loop and never raises into it. `FAUSTUS_LOOP_STALL_SECONDS`
changes the threshold (0 turns the watchdog off).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
import time
import traceback
from collections import deque
from typing import Any, Deque, Dict, List, Optional

logger = logging.getLogger(__name__)

TICK_S = 0.25
POLL_S = 0.5
REDUMP_S = 5.0
MAX_STACK_FRAMES = 22
MAX_KEPT = 25
MAX_LOG_BYTES = 1_000_000

try:
    STALL_S = float(os.environ.get("FAUSTUS_LOOP_STALL_SECONDS", "2") or 0)
except ValueError:
    STALL_S = 2.0

_LOCK = threading.Lock()
_WATCH: Optional["_Watchdog"] = None


def log_path(data_dir: str) -> str:
    return os.path.join(data_dir, "logs", "loop_stalls.jsonl")


def _stack_of(thread_id: int) -> List[str]:
    frame = sys._current_frames().get(thread_id)
    if frame is None:
        return []
    lines = traceback.format_stack(frame)[-MAX_STACK_FRAMES:]
    return [ln.rstrip() for ln in lines]


def _other_threads(skip: int) -> List[Dict[str, str]]:
    """Name and innermost frame of every other thread: who else was running while the loop stood still."""
    names = {t.ident: t.name for t in threading.enumerate()}
    out = []
    for ident, frame in sys._current_frames().items():
        if ident == skip or names.get(ident, "").startswith("loop-watchdog"):
            continue
        out.append({"thread": names.get(ident, str(ident)),
                    "at": f"{os.path.basename(frame.f_code.co_filename)}:{frame.f_lineno} in {frame.f_code.co_name}"})
    return out[:30]


def blocking_call(stack: List[str]) -> str:
    """The innermost frame of the project's own code in a captured stack: the line that held the loop."""
    own = [ln for ln in stack if "site-packages" not in ln and "\\Lib\\" not in ln and "/lib/python" not in ln]
    pick = (own or stack or [""])[-1]
    return " ".join(pick.split())[:240]


class _Watchdog:
    def __init__(self, loop: asyncio.AbstractEventLoop, data_dir: str, stall_s: float) -> None:
        self.loop = loop
        self.path = log_path(data_dir)
        self.stall_s = stall_s
        self.loop_thread = threading.get_ident()
        self.beat = time.monotonic()
        self.max_lag = 0.0
        self.stalls = 0
        self.recent: Deque[Dict[str, Any]] = deque(maxlen=MAX_KEPT)
        self.stop = threading.Event()
        self.task: Optional[asyncio.Task] = None
        self.thread = threading.Thread(target=self._watch, name="loop-watchdog", daemon=True)

    async def _tick(self) -> None:
        expected = time.monotonic() + TICK_S
        while True:
            await asyncio.sleep(TICK_S)
            now = time.monotonic()
            self.max_lag = max(self.max_lag, now - expected)
            self.beat = now
            expected = now + TICK_S

    def start(self) -> None:
        self.beat = time.monotonic()
        self.task = self.loop.create_task(self._tick(), name="loop-watchdog-tick")
        self.thread.start()

    def close(self) -> None:
        self.stop.set()
        if self.task is not None:
            try:
                self.task.cancel()
            except RuntimeError:  # the loop is already closed
                pass

    def _watch(self) -> None:
        current: Optional[Dict[str, Any]] = None
        last_dump = 0.0
        while not self.stop.wait(POLL_S):
            try:
                now = time.monotonic()
                if self.loop.is_closed() or not self.loop.is_running():
                    return  # shut down: a stopped loop is not a stall
                age = now - self.beat
                if age >= self.stall_s:
                    if current is None:
                        current = {"started_at": time.time() - age, "stacks": [], "mono_start": self.beat}
                        last_dump = 0.0
                    if now - last_dump >= REDUMP_S or not current["stacks"]:
                        last_dump = now
                        stack = _stack_of(self.loop_thread)
                        current["stacks"].append({"after_s": round(age, 1), "stack": stack, "call": blocking_call(stack)})
                        current["stacks"] = current["stacks"][-4:]
                        if len(current["stacks"]) == 1:
                            logger.warning("[loop-watchdog] event loop blocked for %.1f s so far, at: %s", age,
                                           current["stacks"][-1]["call"])
                            current["threads"] = _other_threads(self.loop_thread)
                elif current is not None:
                    self._finish(current)
                    current = None
            except Exception:  # noqa: BLE001 - a diagnostic never takes the process down
                logger.debug("loop watchdog poll failed", exc_info=True)

    def _finish(self, stall: Dict[str, Any]) -> None:
        duration = max(0.0, time.monotonic() - stall["mono_start"])
        row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(stall["started_at"])),
               "duration_s": round(duration, 1), "calls": [s["call"] for s in stall["stacks"]],
               "stacks": stall["stacks"], "threads": stall.get("threads", [])}
        self.stalls += 1
        self.recent.append(row)
        logger.warning("[loop-watchdog] event loop was blocked for %.1f s; innermost own frame: %s", duration,
                       row["calls"][-1] if row["calls"] else "?")
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            if os.path.isfile(self.path) and os.path.getsize(self.path) > MAX_LOG_BYTES:
                os.replace(self.path, self.path + ".1")
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            logger.debug("loop watchdog log write failed", exc_info=True)


def install(data_dir: str, *, stall_s: Optional[float] = None) -> bool:
    """Start the watchdog on the running loop. Idempotent; False when it is off. Call from inside the loop."""
    global _WATCH
    limit = STALL_S if stall_s is None else float(stall_s)
    if limit <= 0:
        return False
    with _LOCK:
        if _WATCH is not None and not _WATCH.stop.is_set() and _WATCH.loop.is_running():
            return True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        if _WATCH is not None:
            _WATCH.close()
        _WATCH = _Watchdog(loop, data_dir, limit)
        _WATCH.start()
    return True


def uninstall() -> None:
    global _WATCH
    with _LOCK:
        if _WATCH is not None:
            _WATCH.close()
            _WATCH = None


def status() -> Dict[str, Any]:
    """What this process saw: whether the watchdog is on, the worst tick delay and the latest stalls."""
    w = _WATCH
    if w is None:
        return {"enabled": False, "threshold_s": STALL_S, "stalls": 0, "max_lag_s": 0.0, "recent": []}
    return {"enabled": True, "threshold_s": w.stall_s, "stalls": w.stalls, "max_lag_s": round(w.max_lag, 2),
            "log": w.path, "recent": [_slim(r) for r in list(w.recent)[-10:]]}


def _slim(row: Dict[str, Any]) -> Dict[str, Any]:
    return {"ts": row.get("ts"), "duration_s": row.get("duration_s"), "calls": row.get("calls")}


def read_log(data_dir: str, limit: int = 10, *, full: bool = False) -> List[Dict[str, Any]]:
    """The last `limit` stalls written by any run of the server (newest last)."""
    out: List[Dict[str, Any]] = []
    try:
        with open(log_path(data_dir), "r", encoding="utf-8") as fh:
            lines = fh.readlines()[-max(1, min(int(limit), 100)):]
    except OSError:
        return out
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        out.append(row if full else _slim(row))
    return out
