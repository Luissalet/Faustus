"""Time as a construct the model can reason about.

A model only knows what its context tells it. Without a clock in the tool
results, a command that took 40 minutes and one that took 40 ms read the
same, so the model happily launches another slow one, waits on a server that
will never answer, or "verifies" with a curl it fired 0.2 s after the launch.
Every tool result the model reads therefore starts with one line of wall
time — how long THIS call took, how long the turn has been running, and the
local clock — and a plain-words note when a call was slow.

The same numbers ride on the ``tool_output`` SSE event (``duration_ms``,
``turn_elapsed_ms``) so the Studio can show them without re-deriving them.

Nothing here throws: a missing clock never costs a turn.
"""

from __future__ import annotations

import contextvars
import threading
import time
from collections import OrderedDict
from datetime import datetime
from typing import Any, Dict, Optional


# Monotonic start of the turn currently streaming in this task (contextvar so
# workers and nested loops each carry their own).
_TURN_STARTED: contextvars.ContextVar[float] = contextvars.ContextVar(
    "faustus_turn_started_monotonic", default=0.0
)
_TURN_ROUND: contextvars.ContextVar[int] = contextvars.ContextVar("faustus_turn_round", default=0)

# Above this a call gets an explicit "this was slow" note; the model should
# not fire another one blindly. Below `_FAST_S` the line is kept minimal.
SLOW_CALL_S = 60.0
_FAST_S = 0.5


def enabled() -> bool:
    try:
        from src.settings import get_setting
        return bool(get_setting("agent_tool_wall_time", True))
    except Exception:  # noqa: BLE001
        return True


def begin_turn() -> contextvars.Token:
    return _TURN_STARTED.set(time.monotonic())


def end_turn(token: Optional[contextvars.Token]) -> None:
    if token is None:
        return
    try:
        _TURN_STARTED.reset(token)
    except Exception:  # noqa: BLE001
        _TURN_STARTED.set(0.0)


def set_round(n: int) -> None:
    try:
        _TURN_ROUND.set(int(n or 0))
    except Exception:  # noqa: BLE001
        pass


def turn_elapsed_s() -> Optional[float]:
    t0 = _TURN_STARTED.get()
    if not t0:
        return None
    return max(0.0, time.monotonic() - t0)


def fmt_duration(seconds: Optional[float]) -> str:
    """`0.3s`, `12.4s`, `2m 05s`, `1h 14m` — the shape a person would say."""
    if seconds is None:
        return "?"
    s = max(0.0, float(seconds))
    if s < 10:
        return f"{s:.1f}s"
    if s < 60:
        return f"{int(round(s))}s"
    m, sec = divmod(int(round(s)), 60)
    if m < 60:
        return f"{m}m {sec:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


# Timings live BESIDE the result, never inside it: CALL-05 promises the dict
# a tool returns reaches its caller byte-for-byte, and tests hold it to that.
# Keyed by id(result), popped when read, capped — a result that is never
# formatted costs one slot, not a leak.
_TIMINGS: "OrderedDict[int, Dict[str, Any]]" = OrderedDict()
_TIMINGS_CAP = 256
_TIMINGS_LOCK = threading.Lock()


def _remember(result: Any, timing: Dict[str, Any]) -> None:
    with _TIMINGS_LOCK:
        _TIMINGS[id(result)] = timing
        while len(_TIMINGS) > _TIMINGS_CAP:
            _TIMINGS.popitem(last=False)


def stamp(result: Any, started_monotonic: float, *, started_wall: Optional[float] = None) -> Any:
    """Record timing for a tool result (returns it unchanged). Non-dict
    results are left alone."""
    if not isinstance(result, dict):
        return result
    try:
        elapsed = max(0.0, time.monotonic() - float(started_monotonic))
        wall = float(started_wall) if started_wall else time.time() - elapsed
        te = turn_elapsed_s()
        _remember(result, {
            "started_at": datetime.fromtimestamp(wall).astimezone().isoformat(timespec="seconds"),
            "elapsed_ms": int(elapsed * 1000),
            "turn_elapsed_ms": int(te * 1000) if te is not None else None,
            "round": _TURN_ROUND.get() or None,
        })
    except Exception:  # noqa: BLE001
        pass
    return result


def timing_of(result: Any, *, pop: bool = False) -> Optional[Dict[str, Any]]:
    if not isinstance(result, dict):
        return None
    with _TIMINGS_LOCK:
        if pop:
            return _TIMINGS.pop(id(result), None)
        return _TIMINGS.get(id(result))


def attach(result: Any, timing: Optional[Dict[str, Any]]) -> Any:
    """Carry a timing over to a replacement result (offload/annotation
    builds a new dict); no-op without a timing."""
    if isinstance(result, dict) and timing:
        _remember(result, dict(timing))
    return result


def header_line(result: Any) -> str:
    """The one line the model reads before the body. Empty when the result
    carries no timing or the setting is off."""
    if not enabled():
        return ""
    t = timing_of(result, pop=True)
    if not t:
        return ""
    elapsed_s = (t.get("elapsed_ms") or 0) / 1000.0
    bits = [f"took {fmt_duration(elapsed_s)}"]
    te = t.get("turn_elapsed_ms")
    if te is not None:
        bits.append(f"turn elapsed {fmt_duration(te / 1000.0)}")
    started = str(t.get("started_at") or "")
    if started:
        # 2026-09-16T18:31:07+02:00 → 18:31:07
        clock = started[11:19] if len(started) >= 19 else started
        bits.append(f"started {clock}")
    line = "⏱ " + " · ".join(bits)
    if elapsed_s >= SLOW_CALL_S:
        line += (
            f"\n_This call took {fmt_duration(elapsed_s)} of real time. Do not run it again "
            "to see if it changed; if something must keep running, start it detached (#!bg)."
        )
    return line


def sse_fields(result: Any) -> Dict[str, Any]:
    t = timing_of(result)
    if not t:
        return {}
    out: Dict[str, Any] = {"duration_ms": t.get("elapsed_ms")}
    if t.get("turn_elapsed_ms") is not None:
        out["turn_elapsed_ms"] = t.get("turn_elapsed_ms")
    return out


def turn_clock_note() -> str:
    """A short system-side reminder of how long the turn has run, for the
    loop to append to the round prompt when the turn is long."""
    te = turn_elapsed_s()
    if te is None:
        return ""
    return f"Turn clock: {fmt_duration(te)} elapsed since the user's message."
