"""How long a tool may wait for a job before answering ``{"status": "running", "job_id": ...}``.

Standard library only. The Node twin is ``waitFor`` / ``clampWait`` / ``MAX_WAIT_S`` in ``js/hoard-commons/server.js``.

MCP clients cut a tool call at about 180 s and the job is then orphaned from the caller's point of view. The apps
had every cap between 120 s and 7200 s (Pygmalion 3600, Lumiere 7200, Funes 3600, Galton 600, Prospero 300, Hypatia
120, DiskHoard 150) and bridge timeouts that did not match them. One constant, :data:`MAX_WAIT_S` (150 s, a margin
under the client limit), and one helper, :func:`wait_for`, replace those: a tool clamps ``wait_s``, polls, and
returns the job as it is, with ``still_running`` set when the wait ran out so the agent knows to ask again.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Mapping, Optional, Sequence

__all__ = ["MAX_WAIT_S", "DONE_STATES", "clamp_wait", "wait_for"]

MAX_WAIT_S = 150.0
DONE_STATES: tuple[str, ...] = ("done", "error", "cancelled", "interrupted", "failed")


def clamp_wait(wait_s: Any) -> float:
    """``wait_s`` as a float between 0 and :data:`MAX_WAIT_S`; ``None``, junk, NaN and negatives give 0."""
    try:
        value = float(wait_s)
    except (TypeError, ValueError):
        return 0.0
    if value != value or value < 0:
        return 0.0
    return min(value, MAX_WAIT_S)


def wait_for(get_job: Callable[[], Optional[Mapping[str, Any]]], wait_s: Any, *, poll: float = 0.25,
             done_states: Sequence[str] = DONE_STATES, sleep: Callable[[float], Any] = time.sleep,
             clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Poll ``get_job()`` until its ``state`` (or ``status``) is one of ``done_states`` or ``clamp_wait(wait_s)``
    seconds have passed, and return a copy of the last state it saw, plus ``waited_s``. When it gave up the
    copy also has ``still_running: True``. ``wait_s`` of 0 reads the job once. ``get_job`` returning ``None``
    (the job vanished) gives ``{"state": "missing"}``; an exception from ``get_job`` propagates."""
    limit = clamp_wait(wait_s)
    start = clock()
    while True:
        job = get_job()
        waited = clock() - start
        if job is None:
            return {"state": "missing", "waited_s": round(waited, 2)}
        state = job.get("state", job.get("status"))
        out = dict(job)
        out["waited_s"] = round(waited, 2)
        if state in done_states:
            return out
        if waited >= limit:
            out["still_running"] = True
            return out
        sleep(max(0.0, min(poll, limit - waited)))
