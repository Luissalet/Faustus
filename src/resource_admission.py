"""resource_admission.py — explicit pools of independent resources (ADP-32).

**Measurement pending, by design.** The ficha's own first step is "measure
whether the current serialisation limits real tasks" before building
anything to relax it. That measurement needs production telemetry (how often
two *actually independent* local-model calls land inside the same turn) this
lot has no way to collect offline, so it is declared here as the honest
`unknown` rather than assumed. What this module does instead is the second
step the ficha allows regardless of the measurement's outcome: define the
*shape* pools would have, safely, so that whichever answer the measurement
gives later, wiring it in is a call to `define_pool`/`acquire`, not a
redesign.

What exists today, read (not touched): `src.llm_core._LOCAL_MODEL_LOCK` is
one `asyncio.Lock` shared by every local endpoint, acquired by
`_local_model_slot()` around every local-model call, with a `workload`
argument already distinguishing `"foreground"` from `"background"` (a
foreground request cancels a running background task waiting on the same
lock — see `llm_core._gate_workload`/`_local_model_slot`). That single lock
is provably correct — it can never let two generations collide on one
GPU — and provably conservative: an Ollama on this machine and a second,
unrelated Ollama on the LAN share the exact same lock today, even though
nothing about them is actually contended. This module is NOT wired into
that lock (the ficha is explicit: do not touch it) — seeing how it *would*
connect is in `docs/api/resource_admission.md`.

Reused, not reinvented: pool membership is decided by normalising each
endpoint to the same `scheme://host:port` root `src.vram_admission`'s
`_reservation_key` and `src.endpoint_resolver.normalize_base` already use —
so two URLs that only differ by path (`/v1/chat/completions` vs
`/api/generate`) on the same server can never end up as two pools, which
would quietly double the capacity a caller believes it has (the ficha's
sharpest acceptance criterion).

The gate itself is new and small: `acquire()`/`release()` around a per-pool
`asyncio.Condition`, `max_concurrent=1` (the same safe serial default the
existing lock gives every local call today) unless a pool says otherwise,
and a `"foreground"` waiter is let through before any `"background"` one
still waiting — the same distinction `llm_core` already draws, kept as a
queue-order priority here rather than `llm_core`'s stronger
cancel-the-background-task move, which stays out of scope for a module nine
other lots are not wiring anywhere yet.

Every lease id is unique and single-use: `release()` on an id that was
already released (or never existed) is a no-op, never a double-free and
never a reason to reopen a slot a cancellation already returned. That is
what "a late result does not reopen a finished admission" means here — not
a generation counter, just an id that cannot be reused by construction.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional, Sequence
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

POOL_KINDS = ("gpu", "cpu", "remote")
PRIORITIES = ("foreground", "background")
DEFAULT_MAX_CONCURRENT = 1  # the safe serial default: no pool config means "one at a time"


class PoolError(ValueError):
    """A pool definition or lookup that cannot be honoured."""


class AdmissionTimeout(TimeoutError):
    """`acquire()` waited past its deadline without a slot opening up."""


# ── pools ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Pool:
    pool_id: str
    kind: str
    endpoints: List[str] = field(default_factory=list)
    max_concurrent: int = DEFAULT_MAX_CONCURRENT

    def to_dict(self) -> Dict[str, Any]:
        return {"pool_id": self.pool_id, "kind": self.kind,
                "endpoints": list(self.endpoints), "max_concurrent": self.max_concurrent}


class _PoolState:
    """The live admission counters for one pool — one per `Pool`, created
    lazily the first time anything acquires against it, so defining a pool
    never has to guess an event loop to bind an `asyncio.Condition` to."""

    __slots__ = ("condition", "in_use", "foreground_waiting")

    def __init__(self) -> None:
        self.condition = asyncio.Condition()
        self.in_use = 0
        self.foreground_waiting = 0


_LOCK = threading.RLock()
_POOLS: Dict[str, Pool] = {}
_ENDPOINT_POOL: Dict[str, str] = {}  # normalised endpoint root -> owning pool_id
_STATE: Dict[str, _PoolState] = {}
_LEASES: Dict[str, "Lease"] = {}


def normalize_endpoint(url: str) -> str:
    """`scheme://host:port`, case-folded — the identity two URLs of the same
    server share regardless of which path each one happens to call.

    Delegates the path-suffix stripping to `endpoint_resolver.normalize_base`
    (the same helper `routes`/`llm_core` already use to compare endpoints) and
    then keeps only the network identity, the same granularity
    `vram_admission._reservation_key` reserves VRAM at. Returns "" for
    anything that is not a URL with a host — such a value can never collide
    with a real endpoint, so it is simply not registered as one."""
    raw = str(url or "").strip()
    if not raw:
        return ""
    try:
        from src.endpoint_resolver import normalize_base
        base = normalize_base(raw)
    except Exception:  # noqa: BLE001 - normalisation never blocks admission
        base = raw.rstrip("/")
    try:
        parsed = urlparse(base)
    except ValueError:
        return ""
    if not parsed.scheme or not parsed.hostname:
        return ""
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.hostname}{port}".lower()


def define_pool(pool_id: str, kind: str, endpoints: Sequence[str] = (),
                 *, max_concurrent: Optional[int] = None) -> Dict[str, Any]:
    """Register (or replace) a pool. Raises `PoolError` when an endpoint here
    already belongs to a *different* pool — the one invariant this module
    will not silently violate, because two pools sharing one physical server
    is exactly the "ficticious capacity" the ficha names: `max_concurrent=2`
    on each would let 4 concurrent calls hit hardware that only ever had 2
    slots.

    `endpoints` is normalised and de-duplicated here, once, so a caller that
    hands both `http://host:1234/v1/chat/completions` and
    `http://host:1234/api/generate` gets a pool with exactly one endpoint,
    not two — the same server is the same seat at the table no matter which
    door a request used to knock."""
    pool_id = str(pool_id or "").strip()
    if not pool_id:
        raise PoolError("pool_id is required")
    if kind not in POOL_KINDS:
        raise PoolError(f"kind must be one of {POOL_KINDS}, got {kind!r}")
    try:
        mc = int(max_concurrent) if max_concurrent else DEFAULT_MAX_CONCURRENT
    except (TypeError, ValueError):
        raise PoolError(f"max_concurrent must be an int, got {max_concurrent!r}") from None
    mc = max(1, mc)

    normalised: List[str] = []
    with _LOCK:
        previous = _POOLS.get(pool_id)
        previous_endpoints = set(previous.endpoints) if previous else set()
        for raw in endpoints or ():
            root = normalize_endpoint(raw)
            if not root:
                continue
            owner = _ENDPOINT_POOL.get(root)
            if owner and owner != pool_id:
                raise PoolError(
                    f"endpoint {root!r} already belongs to pool {owner!r}; two URLs of the "
                    "same server must resolve to one pool, not double its capacity")
            if root not in normalised:
                normalised.append(root)
        # Replacing a pool's endpoint list releases the ones it no longer
        # claims, so redefining a pool never leaves a stale claim behind that
        # would block a legitimate new pool from ever using that endpoint.
        for stale in previous_endpoints - set(normalised):
            if _ENDPOINT_POOL.get(stale) == pool_id:
                _ENDPOINT_POOL.pop(stale, None)
        pool = Pool(pool_id=pool_id, kind=kind, endpoints=normalised, max_concurrent=mc)
        _POOLS[pool_id] = pool
        for root in normalised:
            _ENDPOINT_POOL[root] = pool_id
        return pool.to_dict()


def pool_for_endpoint(url: str) -> Optional[str]:
    """The pool a URL's server already belongs to, or None when it is not in
    any pool yet — the lookup `acquire_for_endpoint`-style callers use before
    admitting a call against it."""
    root = normalize_endpoint(url)
    if not root:
        return None
    with _LOCK:
        return _ENDPOINT_POOL.get(root)


def get_pool(pool_id: str) -> Optional[Dict[str, Any]]:
    with _LOCK:
        pool = _POOLS.get(pool_id)
        return pool.to_dict() if pool else None


def _require_pool(pool_id: str) -> Pool:
    with _LOCK:
        pool = _POOLS.get(pool_id)
    if pool is None:
        raise PoolError(f"no such pool: {pool_id!r}")
    return pool


def _state_for(pool_id: str) -> _PoolState:
    with _LOCK:
        state = _STATE.get(pool_id)
        if state is None:
            state = _PoolState()
            _STATE[pool_id] = state
        return state


def forget_pool(pool_id: str) -> None:
    """Drop a pool and its live counters. Tests and a config reload use this;
    it does not touch leases already handed out — those still `release()`
    cleanly, they just no longer have a pool definition to look up capacity
    against, matching how a lease id can never be reused either way."""
    with _LOCK:
        pool = _POOLS.pop(pool_id, None)
        _STATE.pop(pool_id, None)
        if pool:
            for root in pool.endpoints:
                if _ENDPOINT_POOL.get(root) == pool_id:
                    _ENDPOINT_POOL.pop(root, None)


def reset_all() -> None:
    """Tests only: forget every pool, endpoint claim and live counter."""
    with _LOCK:
        _POOLS.clear()
        _ENDPOINT_POOL.clear()
        _STATE.clear()
        _LEASES.clear()


# ── admission ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Lease:
    id: str
    pool_id: str
    priority: str
    owner: str
    acquired_at: float

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "pool_id": self.pool_id, "priority": self.priority,
                "owner": self.owner, "acquired_at": self.acquired_at}


async def acquire(pool_id: str, *, priority: str = "background", owner: str = "",
                   timeout: Optional[float] = None) -> Lease:
    """Wait for a slot in `pool_id` and take it.

    `priority="foreground"` jumps every `"background"` waiter still queued —
    the same distinction `llm_core._gate_workload` already draws for the
    single global lock, kept here as queue order rather than the stronger
    cancel-in-flight move that lock also makes (out of scope: nothing wires
    to this gate yet). `timeout=None` waits indefinitely, matching the
    existing lock's own default; a caller that wants a bound passes one and
    gets `AdmissionTimeout` instead of hanging past it.

    Cancellation is safe by construction: if the awaiting task is cancelled
    while queued, it never reaches the "acquired" branch, so nothing here
    needs to release a slot it never took. Once a `Lease` is returned, the
    caller owns releasing it — `async with lease(...)` below does that in a
    `finally`, including on cancellation of the *held* slot."""
    pool = _require_pool(pool_id)
    priority = priority if priority in PRIORITIES else "background"
    state = _state_for(pool_id)
    deadline = None if timeout is None else time.time() + max(0.0, float(timeout))

    async with state.condition:
        if priority == "foreground":
            state.foreground_waiting += 1
        try:
            def _can_go() -> bool:
                if state.in_use >= pool.max_concurrent:
                    return False
                # A background waiter yields the slot to any foreground
                # waiter still in line, even one that arrived after it.
                if priority == "background" and state.foreground_waiting > 0:
                    return False
                return True

            while not _can_go():
                if deadline is None:
                    await state.condition.wait()
                    continue
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise AdmissionTimeout(
                        f"pool {pool_id!r}: no slot within timeout (max_concurrent="
                        f"{pool.max_concurrent}, in_use={state.in_use})")
                try:
                    await asyncio.wait_for(state.condition.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    raise AdmissionTimeout(
                        f"pool {pool_id!r}: no slot within timeout (max_concurrent="
                        f"{pool.max_concurrent}, in_use={state.in_use})") from None
            state.in_use += 1
        finally:
            if priority == "foreground":
                state.foreground_waiting -= 1

    leased = Lease(id=f"lease-{uuid.uuid4().hex[:12]}", pool_id=pool_id, priority=priority,
                    owner=owner, acquired_at=time.time())
    with _LOCK:
        _LEASES[leased.id] = leased
    return leased


async def release(lease_id: str) -> bool:
    """Free a lease. Returns False when there was nothing to free — an id
    already released, cancelled, or never issued — which is exactly what
    makes a duplicate or late release harmless instead of over-freeing a
    pool's capacity: a second caller reporting the same (already-cancelled)
    result back cannot reopen a slot a cancellation already returned."""
    with _LOCK:
        leased = _LEASES.pop(lease_id, None)
    if leased is None:
        return False
    state = _STATE.get(leased.pool_id)
    if state is None:
        return False
    async with state.condition:
        state.in_use = max(0, state.in_use - 1)
        state.condition.notify_all()
    return True


@asynccontextmanager
async def lease(pool_id: str, *, priority: str = "background", owner: str = "",
                 timeout: Optional[float] = None) -> AsyncIterator[Lease]:
    """`async with resource_admission.lease("pool", priority="foreground"):` —
    acquire, yield, always release, including on cancellation: `finally` runs
    when the `async with` block's task is cancelled too, so a cancelled call
    frees its slot immediately rather than leaking it until nothing else
    ever calls `release()` for it."""
    ticket = await acquire(pool_id, priority=priority, owner=owner, timeout=timeout)
    try:
        yield ticket
    finally:
        await release(ticket.id)


# ── status (GET /api/ops/admission) ─────────────────────────────────────

def status() -> Dict[str, Any]:
    """Every defined pool with its live counters — never raises, read-only."""
    with _LOCK:
        pools = list(_POOLS.values())
    out: List[Dict[str, Any]] = []
    for pool in pools:
        state = _STATE.get(pool.pool_id)
        out.append({
            **pool.to_dict(),
            "in_use": state.in_use if state else 0,
            "foreground_waiting": state.foreground_waiting if state else 0,
            "available": max(0, pool.max_concurrent - (state.in_use if state else 0)),
        })
    return {"pools": out}


__all__ = [
    "POOL_KINDS", "PRIORITIES", "DEFAULT_MAX_CONCURRENT",
    "PoolError", "AdmissionTimeout", "Pool", "Lease",
    "normalize_endpoint", "define_pool", "pool_for_endpoint", "get_pool",
    "forget_pool", "reset_all", "acquire", "release", "lease", "status",
]
