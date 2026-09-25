"""src/swarm/lane.py — the swarm's own lane to a local model server.

`src.llm_core._local_model_slot` serialises every call to a local server
behind ONE process-wide lock: most local servers have a single generation
pipe, and letting a scheduled job and the user's chat share it is how
streams used to cross. A llama-server started with ``-np 4`` (or an Ollama
with ``OLLAMA_NUM_PARALLEL=4``) has four, and a swarm run that went through
that lock would use exactly one of them.

A swarm item runs inside :func:`enter` for the server it was routed to. A
call from that context to the SAME host:port skips the process-wide lock —
the swarm runner's own semaphore already bounds how many of its calls are in
flight to what the server can serve — but it still yields first: it does not
start while a foreground call is waiting for the model or holding it (see
``llm_core._foreground_model_busy``). Every other call, including the swarm's
calls to a different server, goes through the ordinary gate unchanged.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator, Optional
from urllib.parse import urlparse


@dataclass(frozen=True)
class Lane:
    host_key: str
    run_id: str
    parallel: int


_LANE: ContextVar[Optional[Lane]] = ContextVar("faustus_swarm_lane", default=None)


def host_key(url: str) -> str:
    """``http://127.0.0.1:8080/v1/chat/completions`` -> ``127.0.0.1:8080``.
    The path is left out on purpose: a server's native and OpenAI-compatible
    routes (``/api/chat`` vs ``/v1/chat/completions``) are the same slots."""
    try:
        parsed = urlparse(str(url or "").strip())
    except ValueError:
        return ""
    host = (parsed.hostname or "").lower()
    if not host:
        return ""
    if host == "localhost":
        host = "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return f"{host}:{port}"


@contextmanager
def enter(url: str, run_id: str, parallel: int) -> Iterator[Lane]:
    lane = Lane(host_key(url), str(run_id), max(1, int(parallel or 1)))
    token = _LANE.set(lane)
    try:
        yield lane
    finally:
        _LANE.reset(token)


def lane_for(url: str) -> Optional[Lane]:
    """The swarm lane this call belongs to, or None (the ordinary gate)."""
    lane = _LANE.get()
    if lane is None or not lane.host_key:
        return None
    return lane if host_key(url) == lane.host_key else None
