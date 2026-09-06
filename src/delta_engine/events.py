"""delta_engine/events.py -- a comparison as a stream a client may leave and rejoin.

Copied deliberately from `src/state_mirror/events.py`, down to the shape of the
ring buffer: same `since(cursor)` resume, same announcement when the capacity
dropped something, same redaction through the envelope's own `Event.redact`, and
the same UNNAMED SSE frame with the event's name inside the JSON.
`src/contracts/event.py::Event.sse` paid for that last one already -- a named
frame never reaches a page's `onmessage`, so a client written against the
unnamed dispatch stream goes silently deaf on a named one without raising
anything a developer could see.

Keyed by OWNER, like the State Mirror and unlike the council. A delta is not
about a conversation: the same comparison is watched by the page that asked for
it, by the run that will act on its verdict and by whatever opens the record
months later. Keyed by session, each of those would need the same fact published
into every open session -- numbered differently in each, so no two consumers
could agree on a cursor. One stream per owner is the only key under which "what
this comparison has done so far" has one sequence number.

An EMPTY owner is a real key and not a missing one: this install runs
single-user by default and `owner_identity.effective_storage_owner` answers `""`
there, so `""` gets its own stream rather than being folded into a placeholder
shared with every owner that failed to resolve.

Three rules, each with the failure it prevents:

* **An event carries references and counts, never the delta.** A
  `UniversalDelta` is every assertion with its evidence and its alignment; put
  one in `delta_completed` and a comparison over four hundred elements pushes a
  megabyte down an SSE connection to say one word. `delta_id` is on the event
  itself for the same reason `entity_id` is on a `StateEvent`: every consumer
  filters on it, and making them parse each frame to discard most of them is
  the cost nobody notices until there are a thousand frames.

* **No secret leaves in an event.** The payload goes through `Event.redact()`,
  the envelope's own structural redactor, not through a fourth copy of the same
  regexes. It fails CLOSED: a payload that could not be scrubbed is a payload
  that is not published. A delta names paths and quotes `before`/`after` values
  out of somebody's file, so this is not theoretical here.
  `payload["redactions"]` says how many values went, because a field quietly
  dropped is indistinguishable from one that never existed and only one of those
  is safe to reason from.

* **A name this module does not declare is published as `delta_error`.** Never
  dropped, never raised. An unroutable name in a stream is a string nothing
  listens to, and a comparison must not die of a typo in a progress line.

THREADING. The buffer is guarded by a `threading.RLock`, so `publish()` is safe
from any thread -- which matters here because extraction runs on a worker while
a route reads the same stream. Waiters are `asyncio.Event`s remembered with the
loop they were made in and woken through `call_soon_threadsafe` when the
publisher is on another thread: `asyncio.Event.set()` is not thread-safe, and a
lost wake turns every long poll into a full-length timeout that looks exactly
like a comparison that has hung.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, List, Mapping, Optional, Tuple

from src.contracts.base import now_iso
from src.delta_engine.contracts import new_id

logger = logging.getLogger(__name__)

__all__ = [
    "DELTA_EVENTS",
    "COMMON_PAYLOAD_KEYS",
    "DEFAULT_CAPACITY",
    "DeltaEvent",
    "DeltaEventStream",
    "stream_for",
    "close_stream",
    "reset_streams",
    "stream_names",
]

#: The thirteen names this subsystem may publish. Underscored, matching the
#: `project_context_*`, `council_*` and `state_*` blocks already in
#: `src/contracts/event.py::EVENT_NAMES` -- the dotted spelling §24 uses in
#: prose would be a fourth dialect in a tuple that already carries three.
#:
#: Closed for the reason every vocabulary in this package is closed: a name
#: nothing routes on is a string in a log. It must stay a strict SUBSET of
#: `EVENT_NAMES`; a name declared here and missing there would reach a page and
#: then be refused by the envelope an audit replays it through. There is a test
#: that reads `EVENT_NAMES` to check it, rather than repeating the list.
#:
#: `delta_inconclusive` is separate from `delta_completed` on purpose, and
#: `EVENT_NAMES` says why: a comparison that could not see enough to answer is
#: not a comparison that finished, and a consumer waiting for one must not be
#: woken by the other.
DELTA_EVENTS: Tuple[str, ...] = (
    "delta_requested",
    "delta_intent_compiled",
    "delta_source_resolved",
    "delta_extraction_completed",
    "delta_assertion_created",
    "delta_invariant_checked",
    "delta_regression_detected",
    "delta_coverage_computed",
    "delta_completed",
    "delta_inconclusive",
    "delta_reclassified",
    "delta_invalidated",
    "delta_error",
)

#: Section 1.8's common payload, in the spelling this repository already uses
#: (`at` for the timestamp, from `src/contracts/event.py`). Present on every
#: event as `""` when nobody supplied one: absent and empty are two different
#: facts, and a consumer should only ever have to handle one of them.
#:
#: `delta_id`, `request_id`, `intent_contract_id` and `domain` are here rather
#: than in each event's own body because every delta event is about one
#: comparison, of one domain, under one contract -- and a consumer that had to
#: write `payload.get("domain", "")` on every branch eventually forgets a branch.
COMMON_PAYLOAD_KEYS: Tuple[str, ...] = (
    "owner", "project_id", "delta_id", "request_id", "intent_contract_id",
    "domain", "session_id", "run_id", "actor_id", "event_id", "causation_id",
    "correlation_id", "at",
)

#: How many events one owner's stream keeps. A comparison publishes one frame
#: per assertion at its noisiest, so this is several whole deltas of history --
#: enough that a page reloading mid-comparison resumes without a hole, and
#: bounded so that a process left running for a week does not hold a week of
#: them. Past it the oldest are dropped and `since()` says so.
DEFAULT_CAPACITY = 1000

#: The most events one `since()` will hand back, whatever the caller asks for.
MAX_LIMIT = 1000
DEFAULT_LIMIT = 200

_ERROR_EVENT = "delta_error"


def _redact(payload: Mapping[str, Any]) -> Tuple[Dict[str, Any], int]:
    """Scrub the payload with the envelope's own redactor.

    `src/contracts/event.py` owns the only structural redactor on this path;
    `Event` is built here purely to borrow `redact()`, which walks a payload
    blanking secret-looking keys and counting what it blanked. Writing a fifth
    one is how two of them drift apart, and the one that drifts is always the
    one nobody tested.

    Fails CLOSED. If the redactor cannot be reached or throws, the event keeps
    its envelope and loses its payload: a payload we could not scrub is a
    payload we do not publish. A delta payload quotes `before` and `after` out
    of somebody's file, so the thing being protected here is real.
    """
    try:
        from src.contracts.event import Event

        scrubbed = Event(name="delta", data=dict(payload)).redact(())
        return dict(scrubbed.data), int(scrubbed.redactions)
    except Exception as exc:  # noqa: BLE001 - never raise on a publish path
        logger.warning("delta events: redaction unavailable, payload dropped: %s",
                       exc)
        return {"redaction_failed": True}, 0


def _with_common(payload: Dict[str, Any], *, owner: str, event_id: str,
                 created_at: str, late: bool, redactions: int) -> Dict[str, Any]:
    """Fill the common keys in place and return the payload.

    `owner` and `event_id` come from the stream and from the event, never from
    the caller: a publisher that could name someone else's owner could put an
    event in someone else's stream, which is the one mistake in this file with a
    blast radius outside it.
    """
    payload["event_id"] = event_id
    payload["owner"] = owner
    payload["at"] = str(payload.get("at") or created_at)
    for key in COMMON_PAYLOAD_KEYS:
        value = payload.get(key, "")
        payload[key] = "" if value is None else value
    payload["late"] = bool(late)
    payload["redactions"] = int(redactions)
    return payload


@dataclass(frozen=True)
class DeltaEvent:
    """One thing that happened during a comparison, numbered so it can be resumed.

    `seq` is monotonic within an owner's stream and is the resume cursor: a
    client sends back the last one it saw and gets strictly what followed. `id`
    is a separate value on purpose -- it identifies the event across streams and
    in an audit row, while `seq` only means anything inside its own stream.

    `delta_id` is lifted out of the payload and onto the event because every
    consumer of this stream filters on it: a page watching one comparison wants
    that comparison's frames, and making it parse every frame to discard most of
    them is the cost that appears only under load.
    """

    id: str
    seq: int
    name: str
    owner: str
    delta_id: str
    payload: Mapping[str, Any]
    created_at: str
    causation_id: str = ""
    correlation_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "seq": self.seq,
            "name": self.name,
            "owner": self.owner,
            "delta_id": self.delta_id,
            "payload": dict(self.payload),
            "created_at": self.created_at,
            "causation_id": self.causation_id,
            "correlation_id": self.correlation_id,
        }

    def sse(self) -> str:
        """An **unnamed** SSE frame, with the event's name inside the JSON.

        `src/contracts/event.py::Event.sse` explains why and paid for the
        lesson: a named frame never reaches `onmessage`, so a page written
        against the unnamed dispatch stream goes deaf on a named one without
        raising anything a developer could see. This stream is read by the same
        kind of client, so it speaks the same dialect.

        `default=str` because a payload that will not serialise must cost its
        own frame and never the whole stream -- and a delta payload carries
        whatever an adapter put in `extractor_versions` or a threshold, which is
        exactly where an unserialisable value comes from.
        """
        try:
            body = json.dumps(self.to_dict(), ensure_ascii=False,
                              sort_keys=True, default=str)
        except Exception as exc:  # noqa: BLE001 - one bad frame, not a dead stream
            logger.warning("delta events: %s (%s) would not serialise: %s",
                           self.id, self.name, exc)
            body = json.dumps({
                "id": self.id, "seq": self.seq, "name": _ERROR_EVENT,
                "owner": self.owner, "delta_id": self.delta_id,
                "payload": {"unserializable": True},
                "created_at": self.created_at,
                "causation_id": self.causation_id,
                "correlation_id": self.correlation_id,
            }, sort_keys=True)
        return "data: " + body + "\n\n"


class DeltaEventStream:
    """One owner's ordered, resumable, redacted log of what a comparison did.

    In memory and bounded. The durable half lives in `persistence.py` -- the
    delta is the record and this is the live commentary. The two are
    deliberately not the same object: a ring buffer that also had to be a source
    of truth would have to choose between forgetting and growing, and this one
    forgets on purpose.
    """

    def __init__(self, owner: str, *, capacity: int = DEFAULT_CAPACITY) -> None:
        self.owner = str(owner or "")
        self.capacity = max(1, int(capacity or DEFAULT_CAPACITY))
        self._events: Deque[DeltaEvent] = deque(maxlen=self.capacity)
        self._guard = threading.RLock()
        self._waiters: List[Tuple[asyncio.Event, Any]] = []
        self._seq = 0
        self._dropped = 0
        self._late = 0
        self._closed = False

    # -- state ------------------------------------------------------------

    @property
    def closed(self) -> bool:
        with self._guard:
            return self._closed

    def last_seq(self) -> int:
        """The highest sequence number this owner's stream has issued.

        Monotonic for the life of the stream, and the stream outlives its own
        `close()` in the registry, so a client that reconnects after a shutdown
        still resumes from a number that means what it meant before.
        """
        with self._guard:
            return self._seq

    def stats(self) -> Dict[str, Any]:
        with self._guard:
            return {
                "owner": self.owner,
                "capacity": self.capacity,
                "buffered": len(self._events),
                "last_seq": self._seq,
                "dropped": self._dropped,
                "late": self._late,
                "closed": self._closed,
                "waiters": len(self._waiters),
            }

    # -- publishing -------------------------------------------------------

    def publish(self, name: str, **payload: Any) -> DeltaEvent:
        """Number, redact, store and broadcast one event. Never raises.

        An unknown name becomes `delta_error` carrying `unknown_event`: the
        stream's vocabulary stays closed, so nothing downstream has to route on
        a name it has never heard of, and the information is still there for
        whoever has to find the typo. A comparison that died because one
        publisher misspelled a progress line would be a comparison that reports
        nothing about a change that was fine.

        After `close()` the event is still numbered and still stored, but it is
        marked `late` and wakes nobody: what arrived after a shutdown is part of
        the record of what happened, and is acted on by nothing.
        """
        raw = str(name or "").strip()
        known = raw in DELTA_EVENTS
        body = dict(payload)
        if not known:
            logger.warning(
                "delta events %r: %r is not a delta event name; published as %s "
                "so that nothing routes on a name this module never declared",
                self.owner, raw, _ERROR_EVENT)
            body["unknown_event"] = raw
        data, redactions = _redact(body)
        with self._guard:
            self._seq += 1
            seq = self._seq
            late = self._closed
            if late:
                self._late += 1
            if len(self._events) == self.capacity:
                self._dropped += 1
            event = self._make(seq, raw if known else _ERROR_EVENT, data,
                               redactions, late)
            self._events.append(event)
        if late:
            logger.info("delta events %r: %s arrived after close and is kept, "
                        "marked late, and acted on by nothing", self.owner, raw)
            return event
        self._wake()
        return event

    def _make(self, seq: int, name: str, data: Dict[str, Any],
              redactions: int, late: bool) -> DeltaEvent:
        event_id = new_id("delta_event")
        created_at = now_iso()
        payload = _with_common(dict(data), owner=self.owner, event_id=event_id,
                               created_at=created_at, late=late,
                               redactions=redactions)
        return DeltaEvent(
            id=event_id, seq=seq, name=name, owner=self.owner,
            delta_id=str(payload.get("delta_id") or ""),
            payload=payload, created_at=created_at,
            causation_id=str(payload.get("causation_id") or ""),
            correlation_id=str(payload.get("correlation_id") or ""),
        )

    # -- reading ----------------------------------------------------------

    def since(self, cursor: int = 0, *,
              limit: int = DEFAULT_LIMIT) -> Tuple[List[DeltaEvent], int, bool]:
        """`(events, next_cursor, gap)` -- strictly what followed `cursor`.

        "Strictly" is the whole guarantee: a client that reconnects with the
        last cursor it saw receives neither that event again nor anything before
        it, and a client somehow ahead of the stream receives nothing rather
        than a rewind.

        `next_cursor` is the seq of the LAST event in this page, not the head of
        the stream, so a caller that pages through a backlog with a small `limit`
        resumes at the right place instead of skipping everything it did not ask
        for. On an empty page the cursor comes back unchanged rather than reset:
        resetting is how a poller silently starts replaying history.

        `gap` is `True` when the capacity had already dropped events the caller
        asked for. A silent hole is worse than a warning -- the warning costs a
        refresh, and the hole costs a regression nobody knows was missed. It is
        a flag rather than a synthetic event because a marker in the list would
        have to be given a `seq` and a name, and every consumer would then have
        to know which frames are real.
        """
        try:
            after = int(cursor)
        except (TypeError, ValueError):
            logger.debug("delta events %r: unreadable cursor %r, treated as 0",
                         self.owner, cursor)
            after = 0
        if after < 0:
            after = 0
        try:
            cap = int(limit)
        except (TypeError, ValueError):
            cap = DEFAULT_LIMIT
        cap = max(1, min(cap or DEFAULT_LIMIT, MAX_LIMIT))

        with self._guard:
            buffered = list(self._events)
        gap = bool(buffered) and buffered[0].seq > after + 1
        if gap:
            logger.info("delta events %r: consumer resumed at %d but the buffer "
                        "starts at %d; %d event(s) are gone", self.owner, after,
                        buffered[0].seq, (buffered[0].seq - 1) - after)
        out = [event for event in buffered if event.seq > after][:cap]
        return out, (out[-1].seq if out else after), gap

    async def wait(self, cursor: int, *, timeout: float = 15.0) -> bool:
        """Long poll: `True` as soon as something follows `cursor`, `False` on
        the deadline.

        A bool rather than the events themselves, so that the wait and the read
        stay separable: the caller wakes and calls `since()` with whatever cursor
        it holds at that moment, which is the one that is correct if it advanced
        while it was asleep.

        Sleeps on an `asyncio.Event` that `publish()` sets -- there is no poll
        tick and no `sleep(0.1)` loop here. A timeout is not an error: it answers
        `False` and the caller reconnects with the cursor it already had. A
        closed stream answers immediately, so a page that was long-polling
        through a shutdown is not held for the full timeout before it learns.
        """
        events, _, gap = self.since(cursor, limit=1)
        if events or gap or self.closed:
            return True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - a coroutine always has one
            return False
        seconds = None if timeout is None else max(0.0, float(timeout))
        deadline = None if seconds is None else loop.time() + seconds
        waiter = self._subscribe(loop)
        try:
            while True:
                remaining = None if deadline is None else deadline - loop.time()
                if remaining is not None and remaining <= 0:
                    return False
                try:
                    await asyncio.wait_for(waiter.wait(), remaining)
                except asyncio.TimeoutError:
                    return False
                waiter.clear()
                events, _, gap = self.since(cursor, limit=1)
                if events or gap or self.closed:
                    return True
        finally:
            self._unsubscribe(waiter)

    # -- waking the long polls --------------------------------------------

    def _subscribe(self, loop: Any) -> asyncio.Event:
        waiter = asyncio.Event()
        with self._guard:
            self._waiters.append((waiter, loop))
        return waiter

    def _unsubscribe(self, waiter: asyncio.Event) -> None:
        """Called from a `finally` without exception: a client that disconnects
        mid-poll must not leave a waiter behind."""
        with self._guard:
            self._waiters = [(w, l) for (w, l) in self._waiters if w is not waiter]

    def _wake(self) -> None:
        """Set every waiter, from whichever thread we are on.

        `asyncio.Event.set()` is only safe on its own loop's thread; from another
        one the wake can be lost, and a lost wake turns a long poll into a
        full-length timeout that looks exactly like a comparison that has hung.
        Extraction runs on a worker thread, so this path is the normal one here
        rather than the exception.
        """
        with self._guard:
            waiters = list(self._waiters)
        if not waiters:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        for waiter, loop in waiters:
            try:
                if loop is running or loop is None:
                    waiter.set()
                else:
                    loop.call_soon_threadsafe(waiter.set)
            except Exception as exc:  # noqa: BLE001 - a wake never breaks a publish
                logger.debug("delta events %r: waking a waiter failed: %s",
                             self.owner, exc)

    def close(self) -> None:
        """End the stream. Terminal: nothing reopens it.

        For shutdown and for tests. Every waiter is woken so no long poll hangs
        for its full timeout, and every event published afterwards is kept,
        numbered and marked `late`.
        """
        with self._guard:
            if self._closed:
                return
            self._closed = True
        logger.debug("delta events %r: closed at seq %d", self.owner, self.last_seq())
        self._wake()


# -- one stream per owner ---------------------------------------------------
#
# Keyed by owner and kept even after `close()`. A closed stream that was
# forgotten would be recreated by the next publisher with its sequence back at
# 1, and every reconnecting client would then be handed events it had already
# seen under numbers it had already used. `reset_streams()` is the only thing
# that forgets, and it is for tests and shutdown.
#
# The key is the owner verbatim, `""` included: see the module docstring. There
# is no placeholder key here, on purpose.

_STREAMS: Dict[str, DeltaEventStream] = {}
_REGISTRY_GUARD = threading.Lock()


def stream_for(owner: str) -> DeltaEventStream:
    """The stream for this owner, made once and kept."""
    key = str(owner or "")
    with _REGISTRY_GUARD:
        stream: Optional[DeltaEventStream] = _STREAMS.get(key)
        if stream is None:
            stream = _STREAMS[key] = DeltaEventStream(key)
            logger.debug("delta events %r: stream created", key)
        return stream


def close_stream(owner: str) -> None:
    """Close this owner's stream if it has one. The stream stays in the registry
    so its sequence numbers stay meaningful to a client that reconnects."""
    key = str(owner or "")
    with _REGISTRY_GUARD:
        stream: Optional[DeltaEventStream] = _STREAMS.get(key)
    if stream is not None:
        stream.close()


def stream_names() -> Tuple[str, ...]:
    """Every owner with a stream. For diagnostics."""
    with _REGISTRY_GUARD:
        return tuple(sorted(_STREAMS))


def reset_streams() -> None:
    """Forget every stream. For tests and for shutdown; each one is closed first
    so nothing is left waiting on a stream nobody will publish to."""
    with _REGISTRY_GUARD:
        streams = list(_STREAMS.values())
        _STREAMS.clear()
    for stream in streams:
        try:
            stream.close()
        except Exception as exc:  # noqa: BLE001 - shutdown never raises
            logger.debug("delta events: closing %r failed: %s", stream.owner, exc)
