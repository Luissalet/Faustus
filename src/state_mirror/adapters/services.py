"""state_mirror/adapters/services.py -- which subsystems answered, and when.

`service_state.v1` for the things this box runs: the execution backends
`capability_registry` probes, and the instance's own readiness checks. One
observation per service, each stamped with the moment its probe really ran.

**Why not `service_health`.** `src/service_health.py` is the consolidated
report and it is the obvious source, and it is a coroutine. Driving a
coroutine from here would mean starting an event loop inside a synchronous
sweep, and a sweep is called from wherever the scheduler happens to be --
including, sooner or later, from inside a loop already running. That is not a
performance question, it is a deadlock. So this adapter reads the cheap
synchronous sources instead, and when neither of them answers it returns
nothing. An adapter that cannot observe reports nothing; it does not guess.

**Why `disabled` is not `unavailable`.** `service_health` and this schema use
different words for the middle of the range, and the mapping is written down
in `HEALTH_BY_STATUS` below rather than inlined at the two places that need
it. The one row worth arguing about is `disabled -> unknown`. A feature the
user turned off and a feature that is broken look identical in a status
column and are opposite facts: one needs a switch flipped, the other needs
somebody to go and look. Rounding "switched off" to "unavailable" would put a
working machine's deliberate configuration in the same bucket as its faults.

**Why a probe's reason travels and a readiness check's does not.** The
evidence strings `capability_registry` produces are written by this
repository to be shown to a person. The strings `readiness.check_readiness`
produces are raw exception text from SQLAlchemy, and a SQLAlchemy connection
error can carry the database URL, and a database URL can carry a password. A
state row is read by more things than the log it came from, so the readiness
reason stays in the log.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from src.contracts.base import now_iso
from src.state_mirror.adapters.base import (
    Scope,
    ThreadedAdapter,
    entity,
    observation,
)
from src.state_mirror.contracts import StateEntity, StateObservation, entity_id

logger = logging.getLogger(__name__)

__all__ = ["ServicesAdapter", "HEALTH_BY_STATUS", "UNOBSERVED_FIELDS",
           "SOURCE", "SCHEMA"]

SOURCE = "services"
SCHEMA = "service_state.v1"

#: Every word the sources of this adapter use for a service's condition, mapped
#: to the one word `service_state.v1.health` uses. `service_health` says
#: ok|degraded|down|disabled and `capability_registry` says
#: available|unavailable|unknown; both are covered here so the mapping lives in
#: one table rather than in each reader.
#:
#: `disabled -> unknown` is the row that matters. A feature nobody switched on
#: has not been found to be broken; nothing looked at it. Mapping it to
#: `unavailable` would report a deliberate configuration as a fault, and a
#: reader deciding whether to route work somewhere would treat "off" and
#: "down" as the same answer when only one of them can be fixed by starting a
#: daemon.
HEALTH_BY_STATUS: Dict[str, str] = {
    "ok": "available",
    "available": "available",
    "degraded": "degraded",
    "down": "unavailable",
    "unavailable": "unavailable",
    "disabled": "unknown",
    "unknown": "unknown",
}

#: `service_state.v1` fields nothing synchronous on this box can fill, and why.
UNOBSERVED_FIELDS: Dict[str, str] = {
    "latency_ms": "neither source times its probe; capability_registry reports "
                  "a verdict and readiness reports a boolean, and a duration "
                  "measured around the cache would be the cache's",
    "capabilities": "capability_registry.DECLARATIONS is durable intent -- its "
                    "own docstring says so -- not a measurement of the running "
                    "service, and publishing a design promise as an observation "
                    "would say a probe found those capabilities when none did",
}

#: How long a readiness reading stands. `check_readiness` writes and deletes a
#: probe file in DATA_DIR on every call, which is cheap but is still a write,
#: and a sweep every few seconds would put one in the data directory every few
#: seconds for no new information.
READINESS_TTL_SECONDS = 20.0

_lock = threading.RLock()
#: (monotonic time of the read, ISO stamp of the read, report)
_readiness_cache: Optional[Tuple[float, str, Dict[str, Any]]] = None


def reset_cache() -> None:
    """Drop the cached readiness report. For a test, and for the doctor."""
    global _readiness_cache
    with _lock:
        _readiness_cache = None


def health_for(status: Any) -> str:
    """One of `available|degraded|unavailable|unknown` for a source's word.

    A word this build has never heard of answers `unknown` rather than raising.
    An older or newer module reporting a condition this table does not list is
    a reason to say we do not know, and never a reason to break the sweep.
    """
    return HEALTH_BY_STATUS.get(str(status or "").strip().lower(), "unknown")


def read_backends() -> Tuple[Any, ...]:
    """`capability_registry`'s observations, one per declared backend.

    `observe_all()` consults that module's own `_probe_cache`, which is what
    makes this safe to call on a sweep: the docker and ComfyUI probes cost a
    round trip at most once every ten seconds however often this is asked.
    Each observation carries the `checked_at` of the probe behind it, and that
    is what the caller stamps the state with -- so a cached probe ages from
    when it ran, not from when the sweep noticed it.
    """
    from src.capability_registry import observe_all

    return tuple(observe_all(fresh=False))


def _readiness_uncached() -> Dict[str, Any]:
    from src.readiness import check_readiness

    report = check_readiness()
    return report if isinstance(report, dict) else {}


def read_readiness() -> Tuple[str, Dict[str, Any]]:
    """`(observed_at, report)` from `readiness.check_readiness`, cached.

    The stamp is when the checks ran rather than when the sweep asked, for the
    same reason the workspace adapter does it: a cached answer that looked new
    every time it was served would never age out of `fresh`.
    """
    global _readiness_cache
    with _lock:
        hit = _readiness_cache
        if hit is not None and time.monotonic() - hit[0] < READINESS_TTL_SECONDS:
            return hit[1], hit[2]
    stamp = now_iso()
    report = _readiness_uncached()
    with _lock:
        _readiness_cache = (time.monotonic(), stamp, report)
    return stamp, report


def _timestamps(health: str, observed_at: str) -> Dict[str, str]:
    """`last_success_at` / `last_failure_at` for one verdict at one time.

    Only the one the verdict supports is written. Because these observations
    are partial, the other keeps whatever it already held, so a service that
    has just gone down still carries the time it last worked -- which is the
    question somebody asks the moment it stops.
    """
    if health == "available":
        return {"last_success_at": observed_at}
    if health == "unavailable":
        return {"last_failure_at": observed_at}
    return {}


#: How long a probe's evidence sentence may be before it is cut. Long enough
#: for every phrase `capability_registry` composes, short enough that a value
#: nobody expected cannot turn a state row into a document.
REASON_MAX_CHARS = 200

#: The two families of service this adapter can see, as id prefixes. Prefixed
#: rather than bare so that a backend and a readiness check can never collide
#: on a name, and so a reader of an id can tell which source to go back to.
BACKEND_PREFIX = "backend:"
READINESS_PREFIX = "readiness:"

#: Readiness keys that are informational rather than a verdict about a
#: service. `local_first` reports where storage lives -- a remote database is
#: a valid deployment -- and its `ok` is always true, so publishing it as a
#: service that is always available would be a green light nobody earned.
READINESS_IGNORED: Tuple[str, ...] = ("local_first",)


def _backend_rows() -> List[Tuple[str, str, Dict[str, Any]]]:
    """`(identifier, observed_at, state)` for each declared execution backend."""
    rows: List[Tuple[str, str, Dict[str, Any]]] = []
    for obs in read_backends():
        backend_id = str(getattr(obs, "backend_id", "") or "").strip()
        if not backend_id:
            continue
        health = health_for(getattr(obs, "state", ""))
        observed_at = str(getattr(obs, "checked_at", "") or "") or now_iso()
        state: Dict[str, Any] = {"health": health}
        reason = str(getattr(obs, "evidence", "") or "").strip()
        if reason:
            state["reason"] = reason[:REASON_MAX_CHARS]
        state.update(_timestamps(health, observed_at))
        rows.append((BACKEND_PREFIX + backend_id, observed_at, state))
    return rows


def _readiness_rows() -> List[Tuple[str, str, Dict[str, Any]]]:
    """`(identifier, observed_at, state)` for each readiness check.

    No `reason`: see the module docstring. The check's own error text is raw
    exception output and can carry a connection URL.
    """
    observed_at, report = read_readiness()
    checks = report.get("checks")
    if not isinstance(checks, dict):
        return []
    rows: List[Tuple[str, str, Dict[str, Any]]] = []
    for name, result in sorted(checks.items()):
        key = str(name or "").strip()
        if not key or key in READINESS_IGNORED or not isinstance(result, dict):
            continue
        health = "available" if result.get("ok") else "unavailable"
        state: Dict[str, Any] = {"health": health}
        state.update(_timestamps(health, observed_at))
        rows.append((READINESS_PREFIX + key, observed_at, state))
    return rows


class ServicesAdapter(ThreadedAdapter):
    """Execution backends and instance readiness, as `service_state.v1` rows."""

    name = SOURCE
    schemas = (SCHEMA,)

    def _rows(self) -> List[Tuple[str, str, Dict[str, Any]]]:
        """Both families, each failing on its own.

        A readiness check that cannot run must not cost the backend probes: two
        unrelated sources answer this schema and losing one of them is not a
        reason to say nothing about the other.
        """
        rows: List[Tuple[str, str, Dict[str, Any]]] = []
        for reader in (_backend_rows, _readiness_rows):
            rows.extend(self._safe(reader, default=()) or ())
        return rows

    def discover(self, scope: Scope) -> List[StateEntity]:
        out: List[StateEntity] = []
        for identifier, _observed_at, _state in self._safe(self._rows, default=()) or ():
            row = self._safe(entity, "service", identifier, scope=scope,
                             schema=SCHEMA, display_name=identifier,
                             default=None)
            if row is not None:
                out.append(row)
        return out

    def observe(self, scope: Scope) -> List[StateObservation]:
        out: List[StateObservation] = []
        for identifier, observed_at, state in self._safe(self._rows, default=()) or ():
            target = self._safe(entity_id, "service", scope.owner, identifier,
                                namespace=scope.namespace, default="")
            if not target:
                continue
            obs = observation(
                target, SOURCE, state,
                scope=scope,
                schema=SCHEMA,
                # A probe reached the thing, or failed to. Both are what the
                # authoritative system itself answered.
                epistemic="observed",
                observed_at=observed_at,
                # One probe sees one service. It knows nothing about the
                # others, which is exactly why `service_state.v1` is not a
                # snapshot schema (contracts.SNAPSHOT_SCHEMAS).
                partial=True,
            )
            if obs is not None:
                out.append(obs)
        return out
