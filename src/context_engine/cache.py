"""
context_engine/cache.py — L1, the working set, and the one thing it may never do.

§4 gives this layer a short job description: hold the manifests, the token
counts, the candidate lists and the loaded indexes that a turn is about to want
again, with a TTL, a size bound, hit/miss numbers, and a rebuild from L2/L3
when it is lost.  Everything here is derived.  Losing the whole cache costs
latency and nothing else, which is the property that makes it safe to evict
aggressively — and the property this module must not quietly give up by
becoming the only place some piece of state lives.

The failure it is written against is not a stale entry.  It is a leak.  Two
people on one install, one process, one dictionary: a key of `"memories"` is
the same key for both of them, and a cache hit hands the second person the
first person's memories with no store ever having been asked and no
authorisation check ever having run.  So the scope is not part of the value and
not a convention the caller is trusted to follow — it is a mandatory positional
argument of both :meth:`WorkingSet.get` and :meth:`WorkingSet.put`, it is
concatenated into the internal key with a separator that cannot occur in
either half, and `get(scope_b, key)` cannot see what `put(scope_a, key, …)`
wrote.  `tests/test_context_engine_cache.py` pins exactly that.

Two smaller decisions.

**Eviction is LRU plus a TTL plus a byte ceiling, in that order.**  A working
set bounded only by entry count will happily hold four hundred megabytes of
document chunks; one bounded only by bytes will hold a single enormous index
and evict everything a turn actually reuses.  `cost_bytes` is what the caller
says the value costs — this module does not walk an arbitrary object graph to
find out, because doing that on the turn path costs more than the entry saves.
A caller that lies about the cost gets a cache that is bigger than it thinks,
not a crash.

**Invalidation is by event, not by guess.**  §1.7 lists which event invalidates
what, and :func:`on_event` is that table and nothing else.  The alternative — a
short TTL everywhere and hope — is how a packet ends up quoting a project rule
the user deleted two minutes ago, and hope is not a cache policy.  An unknown
event name is not an error: this process may be older than the emitter, and
raising would turn a new event into an outage.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from .contracts import ContextRequest

logger = logging.getLogger(__name__)

#: Separator between scope and key inside the map.  A unit separator cannot
#: appear in a scope built by `ContextExecution.scope_key()` (which joins with
#: `|`) nor in any key this package mints, so no pair of (scope, key) can be
#: made to collide with another by choosing the right punctuation.
SEP = "\x1f"

DEFAULT_MAX_ENTRIES = 512
DEFAULT_MAX_BYTES = 32_000_000
DEFAULT_TTL_S = 300.0

#: Key prefixes, so that an event can invalidate one *kind* of entry instead of
#: a whole scope.  §1.7 asks for exactly this granularity: a state projection
#: changing must not throw away the session's tokenisation cache.
PREFIX_CANDIDATES = "candidates:"
PREFIX_PLAN = "plan:"
PREFIX_PACKET = "packet:"
PREFIX_REQUEST = "request:"
PREFIX_BLOCKS = "blocks:"
PREFIX_STATE = "state:"
PREFIX_TOOLS = "tools:"

KEY_PREFIXES: Tuple[str, ...] = (
    PREFIX_CANDIDATES, PREFIX_PLAN, PREFIX_PACKET, PREFIX_REQUEST,
    PREFIX_BLOCKS, PREFIX_STATE, PREFIX_TOOLS,
)


@dataclass(frozen=True)
class CacheStats:
    """Hit rate and size, which are the two numbers that say whether this layer
    is earning its memory.  §4 asks for them by name."""

    hits: int = 0
    misses: int = 0
    evictions: int = 0
    entries: int = 0
    bytes: int = 0

    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return round(self.hits / total, 6) if total else 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {"hits": self.hits, "misses": self.misses,
                "evictions": self.evictions, "entries": self.entries,
                "bytes": self.bytes, "hit_rate": self.hit_rate()}


@dataclass
class _Entry:
    value: Any
    expires_at: float
    cost_bytes: int
    scope: str
    key: str


class WorkingSet:
    """A scoped, bounded, expiring map from `(scope, key)` to anything.

    Thread-safe: the compiler runs its sources in threads and the maintenance
    pass runs on another one entirely, so an `OrderedDict` mutated from both
    without a lock is a `RuntimeError` in the middle of a turn.
    """

    def __init__(self, *, max_entries: int = DEFAULT_MAX_ENTRIES,
                 max_bytes: int = DEFAULT_MAX_BYTES,
                 ttl_s: float = DEFAULT_TTL_S,
                 clock: Optional[Callable[[], float]] = None) -> None:
        self._lock = threading.RLock()
        self._entries: "OrderedDict[str, _Entry]" = OrderedDict()
        self._clock = clock or time.monotonic
        self._max_entries = max(1, int(max_entries or DEFAULT_MAX_ENTRIES))
        self._max_bytes = max(0, int(max_bytes or 0))
        self._ttl_s = float(ttl_s if ttl_s and ttl_s > 0 else DEFAULT_TTL_S)
        self._bytes = 0
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    # ── keys ───────────────────────────────────────────────────────────────

    @staticmethod
    def _compose(scope: str, key: str) -> str:
        return f"{scope or ''}{SEP}{key or ''}"

    @staticmethod
    def _split(composed: str) -> Tuple[str, str]:
        scope, _, key = composed.partition(SEP)
        return scope, key

    # ── read and write ─────────────────────────────────────────────────────

    def get(self, scope: str, key: str) -> Optional[Any]:
        """The value stored under this exact `(scope, key)`, or None.

        None is also what an expired entry answers, and the entry is dropped on
        the way out: an expiry that only happens during eviction means a cache
        under no pressure serves stale rows forever."""
        composed = self._compose(scope, key)
        with self._lock:
            entry = self._entries.get(composed)
            if entry is None:
                self._misses += 1
                return None
            if entry.expires_at <= self._clock():
                self._drop(composed)
                self._misses += 1
                return None
            self._entries.move_to_end(composed)
            self._hits += 1
            return entry.value

    def put(self, scope: str, key: str, value: Any, *, cost_bytes: int = 0,
            ttl_s: Optional[float] = None) -> None:
        """Store a value under `(scope, key)`.  Never raises.

        An entry whose own `cost_bytes` exceeds the whole ceiling is not
        stored: keeping it would evict everything else and then be evicted
        itself on the next write, which is a cache that only ever costs."""
        composed = self._compose(scope, key)
        try:
            cost = max(0, int(cost_bytes or 0))
        except (TypeError, ValueError):
            cost = 0
        try:
            life = float(ttl_s) if ttl_s is not None else self._ttl_s
        except (TypeError, ValueError):
            life = self._ttl_s
        if life <= 0:
            life = self._ttl_s

        with self._lock:
            if self._max_bytes and cost > self._max_bytes:
                logger.debug("working set refused %s: %d bytes over the ceiling",
                             key, cost)
                self._drop(composed)
                return
            self._drop(composed)
            self._entries[composed] = _Entry(value=value,
                                             expires_at=self._clock() + life,
                                             cost_bytes=cost, scope=scope or "",
                                             key=key or "")
            self._bytes += cost
            self._enforce()

    def invalidate(self, scope: str = "", *, key: str = "",
                   prefix: str = "") -> int:
        """Forget entries, and say how many.  Four shapes, narrowest first:

        * `key` given: that one entry in that scope (or in every scope, when
          `scope` is empty — a global id such as a packet id);
        * `prefix` given: every key starting with it, in that scope or in all;
        * `scope` alone: everything that scope holds;
        * nothing at all: the whole cache.
        """
        with self._lock:
            victims: List[str] = []
            for composed, entry in self._entries.items():
                if scope and entry.scope != scope:
                    continue
                if key and entry.key != key:
                    continue
                if prefix and not entry.key.startswith(prefix):
                    continue
                victims.append(composed)
            for composed in victims:
                self._drop(composed)
            return len(victims)

    def stats(self) -> CacheStats:
        with self._lock:
            self._expire()
            return CacheStats(hits=self._hits, misses=self._misses,
                              evictions=self._evictions,
                              entries=len(self._entries), bytes=self._bytes)

    def clear(self) -> None:
        """Empty it, counters included.  A cleared working set is not a cache
        with a bad hit rate; it is a cache that has not been asked yet."""
        with self._lock:
            self._entries.clear()
            self._bytes = 0
            self._hits = 0
            self._misses = 0
            self._evictions = 0

    def scopes(self) -> Tuple[str, ...]:
        """Every scope currently holding something.  `on_event` walks these to
        find the ones a project-level event touches, because a scope key also
        carries the council and branch and cannot be rebuilt from a payload."""
        with self._lock:
            return tuple(sorted({entry.scope for entry in self._entries.values()}))

    # ── internals ──────────────────────────────────────────────────────────

    def _drop(self, composed: str) -> None:
        entry = self._entries.pop(composed, None)
        if entry is not None:
            self._bytes = max(0, self._bytes - entry.cost_bytes)

    def _expire(self) -> None:
        now = self._clock()
        for composed in [c for c, e in self._entries.items() if e.expires_at <= now]:
            self._drop(composed)

    def _enforce(self) -> None:
        self._expire()
        while len(self._entries) > self._max_entries:
            self._evict_oldest()
        while self._max_bytes and self._bytes > self._max_bytes and self._entries:
            self._evict_oldest()

    def _evict_oldest(self) -> None:
        composed, entry = self._entries.popitem(last=False)
        self._bytes = max(0, self._bytes - entry.cost_bytes)
        self._evictions += 1


# ── the process singleton ──────────────────────────────────────────────────

_LOCK = threading.RLock()
_WORKING_SET: Optional[WorkingSet] = None


def _configured_entries() -> int:
    try:
        from src.settings import get_setting

        value = int(get_setting("agent_context_cache_entries", DEFAULT_MAX_ENTRIES))
    except Exception:  # noqa: BLE001 - an unreadable setting is the default
        return DEFAULT_MAX_ENTRIES
    return value if value > 0 else DEFAULT_MAX_ENTRIES


def working_set() -> WorkingSet:
    """The process's working set, built on first use.

    One per process and not one per compiler: the whole point is that the
    second turn finds what the first one paid for, and a per-instance cache
    would be thrown away with the instance."""
    global _WORKING_SET
    with _LOCK:
        if _WORKING_SET is None:
            _WORKING_SET = WorkingSet(max_entries=_configured_entries())
        return _WORKING_SET


def reset_working_set() -> None:
    """Drop the singleton.  For tests, and for a process that has just been
    reconfigured — a new data dir means the old entries describe another
    install."""
    global _WORKING_SET
    with _LOCK:
        _WORKING_SET = None


def scope_of(request: ContextRequest) -> str:
    """The cache scope for a request: `ContextExecution.scope_key()` and
    nothing else, so that two modules cannot disagree about what "the same
    scope" means."""
    try:
        return request.execution.scope_key()
    except Exception:  # noqa: BLE001 - a malformed request gets its own scope
        logger.debug("context cache could not read a scope key; isolating")
        return SEP + "unscoped"


# ── §1.7: the event table ──────────────────────────────────────────────────

#: `event name -> key prefixes to forget`.  An empty tuple means "the whole
#: scope": the event changed something the packet as a whole depends on and
#: there is no honest way to keep half of it.
EVENT_PREFIXES: Dict[str, Tuple[str, ...]] = {
    # Project context: a source arrived, changed or left.  Everything derived
    # from that project's sources is now a claim about a revision that moved.
    "project_context_attached": (),
    "project_context_updated": (),
    "project_context_detached": (),
    # A projection is operational state.  It invalidates the state block and
    # the blocks assembled beside it, and nothing else: throwing away the
    # session's candidates because a file watcher ticked is how a cache stops
    # being worth having.
    "state_projection_updated": (PREFIX_STATE, PREFIX_BLOCKS),
    # Skill/model/workflow health changed: the tool guidance is what said a
    # capability was usable, so that is what stops being true.
    "capability_health_changed": (PREFIX_TOOLS,),
    # A certified procedure becomes recommendable, which changes both the tool
    # guidance and what the experience lane would return.
    "procedure_certified": (PREFIX_TOOLS, PREFIX_CANDIDATES),
    # A branch was chosen: its scope's compiled packets described a future that
    # is now either the present or discarded.
    "branch_selected": (),
    # A decision is binding and belongs in a mandatory section from now on.
    "council_decision_recorded": (),
    # The final transcript replaces the partial hypotheses the turn was
    # compiled against.
    "voice_turn_committed": (PREFIX_CANDIDATES, PREFIX_PACKET),
    # A delta is new recoverable evidence for the project.
    "semantic_delta_created": (PREFIX_CANDIDATES,),
}

#: Events whose payload names a whole install rather than one owner: their
#: invalidation is not scoped, because capability health is not per person.
GLOBAL_EVENTS: Tuple[str, ...] = ("capability_health_changed",)


def _payload_scope_prefix(payload: Mapping[str, Any]) -> str:
    """The leading half of the scope keys an event touches.

    `ContextExecution.scope_key()` is `owner|project|council|branch`, and an
    event payload carries at most the first two.  Matching on the prefix is
    what lets one `project_context_updated` reach the council and branch scopes
    of that same project, which are separate scopes and equally stale."""
    owner = str(payload.get("owner") or "")
    project = str(payload.get("project_id") or payload.get("workspace") or "")
    return f"{owner}|{project}"


def on_event(name: str, payload: Mapping[str, Any]) -> int:
    """React to one of §1.7's events.  Returns how many entries were forgotten.

    Never raises and never fails a caller: this is called from emitters that
    have already done their real work, and an exception here would roll back
    somebody else's commit for the sake of a cache."""
    event = str(name or "").strip()
    table = EVENT_PREFIXES.get(event)
    if table is None:
        logger.debug("context cache ignoring unknown event %r", event)
        return 0

    data: Mapping[str, Any] = payload if isinstance(payload, Mapping) else {}
    cache = working_set()
    try:
        if event in GLOBAL_EVENTS:
            return sum(cache.invalidate(prefix=prefix) for prefix in table)

        wanted = _payload_scope_prefix(data)
        if wanted == "|":
            # No owner and no project: an event we cannot place.  Forgetting
            # everything on the strength of an empty payload would let one
            # malformed emitter flush the cache on every tick.
            logger.debug("context cache: %s carried no owner or project", event)
            return 0

        dropped = 0
        for scope in cache.scopes():
            if not scope.startswith(wanted):
                continue
            if not table:
                dropped += cache.invalidate(scope)
                continue
            for prefix in table:
                dropped += cache.invalidate(scope, prefix=prefix)
        return dropped
    except Exception:  # noqa: BLE001 - see the docstring
        logger.warning("context cache event %r failed", event, exc_info=True)
        return 0


__all__ = [
    "SEP", "DEFAULT_MAX_ENTRIES", "DEFAULT_MAX_BYTES", "DEFAULT_TTL_S",
    "PREFIX_CANDIDATES", "PREFIX_PLAN", "PREFIX_PACKET", "PREFIX_REQUEST",
    "PREFIX_BLOCKS", "PREFIX_STATE", "PREFIX_TOOLS", "KEY_PREFIXES",
    "EVENT_PREFIXES", "GLOBAL_EVENTS",
    "CacheStats", "WorkingSet", "working_set", "reset_working_set",
    "scope_of", "on_event",
]
