"""state_mirror/events.py -- the machine as a stream a client may leave and rejoin.

Copied deliberately from `src/council/events.py`, down to the shape of the ring
buffer: same `since(seq)` resume, same gap marker on overflow, same redaction
through the envelope's own `Event.redact`, and the same UNNAMED SSE frame with
the event's name inside the JSON. `src/contracts/event.py::Event.sse` paid for
that last one already -- a named frame never reaches a page's `onmessage`, so a
client written against the unnamed dispatch stream goes silently deaf on a
named one, without raising anything a developer could see.

The ONE structural difference from the council's stream, and the reason this is
a file rather than a `stream_for(session_id)` call:

**The stream is keyed by OWNER, not by session.** A council event is about a
conversation and dies with it. A state event is about a MACHINE. The disk
filling up, a service going down and a run finishing are facts about the box,
not about whichever chat happened to be open when a sweep noticed them. Keyed
by session, each of those would have to be published into every open session --
the same fact numbered differently in each, so no two consumers could agree on
a cursor -- or into whichever session the sweep ran under, where a page that
reloaded into a new session would never see it at all. One stream per owner is
the only key under which "what is true right now" has one sequence number.

An EMPTY owner is a real key here and not a missing one. This install runs
single-user by default and `owner_identity.effective_storage_owner` answers
`""` there, so `""` gets its own stream instead of being folded into a
placeholder -- folding it would put a single-user install's entire state stream
into a bucket shared with every owner that failed to resolve.

Three rules this file holds, each with the failure it prevents:

* **`state_changed` carries a compact diff and the revision, never the state.**
  A `MaterializedState` is every field with its four pieces of metadata; put
  one on every change and a sweep that touched forty entities pushes a
  megabyte through an SSE connection for forty facts that moved. The diff comes
  from `reducers.FieldChange`, which already answers "status went running ->
  completed", and the revision is what Delta Engine compares.

* **No secret leaves in an event.** The payload goes through `Event.redact()`,
  the envelope's own structural redactor, and not through a fourth copy of the
  same regexes. It fails CLOSED: a payload that could not be scrubbed is a
  payload that is not published. `payload["redactions"]` says how many values
  went, because a field quietly dropped is indistinguishable from one that
  never existed and only one of those is safe to reason from.

* **A name this module does not declare is published as `state_error`.** Never
  dropped, never raised. An unroutable name in a stream is a string nothing
  listens to, and a sweep must not die of a typo in a log line.

THREADING. The buffer is guarded by a `threading.RLock`, so `publish()` is safe
from any thread -- which matters more here than in the council, because sweeps
run on background threads while a route reads the same stream. Waiters are
`asyncio.Event`s remembered with the loop they were made in and woken through
`call_soon_threadsafe` when the publisher is on another thread:
`asyncio.Event.set()` is not thread-safe, and a lost wake turns every long poll
into a full-length timeout that looks exactly like a stalled machine.
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
from src.state_mirror.contracts import new_id

logger = logging.getLogger(__name__)

__all__ = [
    "STATE_EVENTS",
    "COMMON_PAYLOAD_KEYS",
    "DEFAULT_CAPACITY",
    "StateEvent",
    "StateEventStream",
    "stream_for",
    "close_stream",
    "reset_streams",
    "stream_names",
]


#: The twelve names this subsystem may publish. Underscored, matching the
#: `project_context_*` and `council_*` precedent already in
#: `src/contracts/event.py::EVENT_NAMES` -- the dotted spelling the plan uses
#: in prose would be a third dialect in a tuple that already has two, and the
#: context cache and the audit both route on the underscored one.
#:
#: Closed for the reason every vocabulary in this package is closed: a name
#: nothing routes on is a string in a log, and a name in this tuple is
#: something a page, a hook and an audit all understand. It must stay a SUBSET
#: of `EVENT_NAMES`; a name declared here and missing there would reach a page
#: and then be refused by the envelope an audit replays it through.
STATE_EVENTS: Tuple[str, ...] = (
    "state_entity_discovered",
    "state_observation_received",
    "state_changed",
    "state_became_stale",
    "state_conflict_detected",
    "state_conflict_resolved",
    "state_entity_retired",
    "state_reconcile_started",
    "state_reconcile_completed",
    "state_source_degraded",
    "state_source_recovered",
    "state_error",
)

#: Section 1.8's common payload, in the spelling this repository already uses
#: (`at` for the timestamp, from `src/contracts/event.py`). Present on every
#: event as `""` when nobody supplied one: absent and empty are two different
#: facts, and a consumer should only ever have to handle one of them.
#:
#: `entity_id` and `source` are here rather than in each event's own body
#: because every state event is about one entity as seen by one source, and a
#: consumer that had to write `payload.get("entity_id", "")` on every branch
#: eventually forgets a branch.
COMMON_PAYLOAD_KEYS: Tuple[str, ...] = (
    "owner", "project_id", "namespace", "entity_id", "source", "session_id",
    "run_id", "actor_id", "event_id", "causation_id", "correlation_id", "at",
)

#: How many events one owner's stream keeps. A sweep across eleven adapters
#: publishes a few hundred, so this is several sweeps of history -- enough that
#: a page reloading between sweeps resumes without a hole, and bounded so that
#: a machine left running for a week does not keep a week of them in memory.
#: Past it the oldest are dropped and `since()` announces the hole.
DEFAULT_CAPACITY = 2000

#: The most events one `since()` will hand back, whatever the caller asks for.
MAX_LIMIT = 1000
DEFAULT_LIMIT = 200

_ERROR_EVENT = "state_error"


def _redact(payload: Mapping[str, Any]) -> Tuple[Dict[str, Any], int]:
    """Scrub the payload with the envelope's own redactor.

    `src/contracts/event.py` owns the only structural redactor on this path;
    `Event` is built here purely to borrow `redact()`, which walks a payload
    blanking secret-looking keys and counting what it blanked. Writing a fourth
    one is how two of them drift apart, and the one that drifts is always the
    one nobody tested.

    Fails CLOSED. If the redactor cannot be reached or throws, the event keeps
    its envelope and loses its payload: a payload we could not scrub is a
    payload we do not publish. State values are not secrets by design -- no
    credential enters this store -- but "by design" is a claim about every
    adapter ever written, and this is the line that does not depend on it.
    """
    try:
        from src.contracts.event import Event

        scrubbed = Event(name="state", data=dict(payload)).redact(())
        return dict(scrubbed.data), int(scrubbed.redactions)
    except Exception as exc:  # noqa: BLE001 - never raise on a publish path
        logger.warning("state events: redaction unavailable, payload dropped: %s", exc)
        return {"redaction_failed": True}, 0


def _with_common(payload: Dict[str, Any], *, owner: str, event_id: str,
                 created_at: str, late: bool, redactions: int) -> Dict[str, Any]:
    """Fill the common keys in place and return the payload.

    `owner` and `event_id` come from the stream and from the event, never from
    the caller: a publisher that could name someone else's owner could put an
    event in someone else's stream, which is the one mistake in this file with
    a blast radius outside it.
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
class StateEvent:
    """One thing that changed about the machine, numbered so it can be resumed.

    `seq` is monotonic within an owner's stream and is the resume cursor: a
    client sends back the last one it saw and gets strictly what followed. `id`
    is a separate value on purpose -- it identifies the event across streams
    and in an audit row, while `seq` only means anything inside its own stream.

    `entity_id` is lifted out of the payload and onto the event because every
    consumer of this stream filters on it: a page showing one run wants the
    events about that run, and making it dig through the payload to find out
    would mean parsing every frame to discard most of them.
    """

    id: str
    seq: int
    name: str
    owner: str
    entity_id: str
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
            "entity_id": self.entity_id,
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
        own frame and never the whole stream -- and a state payload carries
        whatever an adapter observed, which is exactly where an unserialisable
        value comes from.
        """
        try:
            body = json.dumps(self.to_dict(), ensure_ascii=False,
                              sort_keys=True, default=str)
        except Exception as exc:  # noqa: BLE001 - one bad frame, not a dead stream
            logger.warning("state events: %s (%s) would not serialise: %s",
                           self.id, self.name, exc)
            body = json.dumps({
                "id": self.id, "seq": self.seq, "name": _ERROR_EVENT,
                "owner": self.owner, "entity_id": self.entity_id,
                "payload": {"unserializable": True},
                "created_at": self.created_at,
                "causation_id": self.causation_id,
                "correlation_id": self.correlation_id,
            }, sort_keys=True)
        return "data: " + body + "\n\n"


class StateEventStream:
    """One owner's ordered, resumable, redacted log of what changed.

    In memory and bounded. The durable half lives in `persistence.py` -- the
    observation log and the materialised state are the record, and this is the
    live stream a page reads. The two are deliberately not the same object: a
    ring buffer that also had to be a source of truth would have to choose
    between forgetting and growing, and this one forgets on purpose.
    """

    def __init__(self, owner: str, *, capacity: int = DEFAULT_CAPACITY) -> None:
        self.owner = str(owner or "")
        self.capacity = max(1, int(capacity or DEFAULT_CAPACITY))
        self._events: Deque[StateEvent] = deque(maxlen=self.capacity)
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

    def publish(self, name: str, **payload: Any) -> StateEvent:
        """Number, redact, store and broadcast one event. Never raises.

        An unknown name becomes `state_error` carrying `unknown_event`: the
        stream's vocabulary stays closed, so nothing downstream has to route on
        a name it has never heard of, and the information is still there for
        whoever has to find the typo. A sweep that died because one publisher
        misspelled a name would be a sweep that reports nothing about a machine
        that is fine.

        After `close()` the event is still numbered and still stored, but it is
        marked `late` and wakes nobody: what arrived after a shutdown is part
        of the record of what happened, and is acted on by nothing.
        """
        raw = str(name or "").strip()
        known = raw in STATE_EVENTS
        body = dict(payload)
        if not known:
            logger.warning(
                "state events %r: %r is not a state event name; published as %s "
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
            logger.info("state events %r: %s arrived after close and is kept, "
                        "marked late, and acted on by nothing", self.owner, raw)
            return event
        self._wake()
        return event

    def _make(self, seq: int, name: str, data: Dict[str, Any],
              redactions: int, late: bool) -> StateEvent:
        event_id = new_id("sev")
        created_at = now_iso()
        payload = _with_common(dict(data), owner=self.owner, event_id=event_id,
                               created_at=created_at, late=late,
                               redactions=redactions)
        return StateEvent(
            id=event_id, seq=seq, name=name, owner=self.owner,
            entity_id=str(payload.get("entity_id") or ""),
            payload=payload, created_at=created_at,
            causation_id=str(payload.get("causation_id") or ""),
            correlation_id=str(payload.get("correlation_id") or ""),
        )

    # -- reading ----------------------------------------------------------

    def since(self, seq: int, *, limit: int = DEFAULT_LIMIT) -> List[StateEvent]:
        """Strictly what follows `seq`, oldest first, at most `limit` of them.

        "Strictly" is the whole guarantee: a client that reconnects with the
        last id it saw receives neither that event again nor anything before
        it. A client somehow ahead of the stream receives nothing rather than a
        rewind.

        When the buffer has already dropped what the caller asks for, the list
        STARTS with a `state_error` gap marker naming the range that is gone. A
        silent hole is worse than a warning: the warning costs a refresh, and
        the hole costs a state change nobody knows was missed. The marker is
        built, not published -- it consumes no sequence number and is not
        stored, so two clients resuming from the same place see the same stream
        and neither of them shifts it.
        """
        try:
            after = int(seq)
        except (TypeError, ValueError):
            logger.debug("state events %r: unreadable cursor %r, treated as 0",
                         self.owner, seq)
            after = 0
        if after < 0:
            after = 0
        try:
            cap = int(limit)
        except (TypeError, ValueError):
            cap = DEFAULT_LIMIT
        cap = max(1, min(cap, MAX_LIMIT))

        with self._guard:
            buffered = list(self._events)
        out = [event for event in buffered if event.seq > after]
        if buffered:
            oldest = buffered[0].seq
            if after < oldest - 1:
                missed = (oldest - 1) - after
                logger.info("state events %r: consumer resumed at %d but the "
                            "buffer starts at %d; %d event(s) are gone",
                            self.owner, after, oldest, missed)
                out.insert(0, self._gap(after, oldest, missed))
        return out[:cap]

    def _gap(self, after: int, oldest: int, missed: int) -> StateEvent:
        """The marker that says a hole is a hole.

        Its `seq` is `oldest - 1`, so a consumer that stores the last seq it
        saw lands exactly where the surviving events begin instead of asking
        for the missing ones again on every reconnect.
        """
        event_id = new_id("sev")
        created_at = now_iso()
        payload = _with_common(
            {"gap": True, "missed": int(missed), "from_seq": after + 1,
             "to_seq": oldest - 1, "capacity": self.capacity,
             "reason": "buffer_overflow"},
            owner=self.owner, event_id=event_id, created_at=created_at,
            late=False, redactions=0)
        return StateEvent(id=event_id, seq=max(0, oldest - 1),
                          name=_ERROR_EVENT, owner=self.owner, entity_id="",
                          payload=payload, created_at=created_at)

    async def wait(self, seq: int, *, timeout_s: float = 25.0) -> List[StateEvent]:
        """Long poll: answer as soon as there is anything after `seq`, or `[]`
        when the deadline passes.

        Sleeps on an `asyncio.Event` that `publish()` sets -- there is no poll
        tick and no `sleep(0.1)` loop here. A timeout is not an error: it
        answers with an empty list, and the caller reconnects with the cursor
        it already had.

        A closed stream answers immediately with whatever is left, so a page
        that was long-polling through a shutdown is not held for the full
        timeout before it learns that.
        """
        ready = self.since(seq)
        if ready or self.closed:
            return ready
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - a coroutine always has one
            return []
        timeout = None if timeout_s is None else max(0.0, float(timeout_s))
        deadline = None if timeout is None else loop.time() + timeout
        waiter = self._subscribe(loop)
        try:
            while True:
                remaining = None if deadline is None else deadline - loop.time()
                if remaining is not None and remaining <= 0:
                    return []
                try:
                    await asyncio.wait_for(waiter.wait(), remaining)
                except asyncio.TimeoutError:
                    return []
                waiter.clear()
                ready = self.since(seq)
                if ready or self.closed:
                    return ready
        finally:
            self._unsubscribe(waiter)

    # -- waking the long polls --------------------------------------------

    def _subscribe(self, loop: Any) -> asyncio.Event:
        waiter = asyncio.Event()
        with self._guard:
            self._waiters.append((waiter, loop))
        return waiter

    def _unsubscribe(self, waiter: asyncio.Event) -> None:
        """Called from a `finally` without exception: a client that
        disconnects mid-poll must not leave a waiter behind."""
        with self._guard:
            self._waiters = [(w, l) for (w, l) in self._waiters if w is not waiter]

    def _wake(self) -> None:
        """Set every waiter, from whichever thread we are on.

        `asyncio.Event.set()` is only safe on its own loop's thread; from
        another one the wake can be lost, and a lost wake turns a long poll
        into a full-length timeout that looks exactly like a machine nothing is
        happening on. Sweeps run on background threads, so this path is the
        normal one here rather than the exception.
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
                logger.debug("state events %r: waking a waiter failed: %s",
                             self.owner, exc)

    def close(self) -> None:
        """End the stream. Terminal: nothing reopens it.

        For shutdown and for tests, not for anything a user does -- an owner's
        machine does not stop existing the way a council room ends. Every
        waiter is woken so no long poll hangs for its full timeout, and every
        event published afterwards is kept, numbered and marked `late`.
        """
        with self._guard:
            if self._closed:
                return
            self._closed = True
        logger.debug("state events %r: closed at seq %d", self.owner, self.last_seq())
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

_STREAMS: Dict[str, StateEventStream] = {}
_REGISTRY_GUARD = threading.Lock()


def stream_for(owner: str) -> StateEventStream:
    """The stream for this owner, made once and kept."""
    key = str(owner or "")
    with _REGISTRY_GUARD:
        stream = _STREAMS.get(key)
        if stream is None:
            stream = _STREAMS[key] = StateEventStream(key)
            logger.debug("state events %r: stream created", key)
        return stream


def close_stream(owner: str) -> None:
    """Close this owner's stream if it has one. The stream stays in the
    registry so its sequence numbers stay meaningful to a client that
    reconnects afterwards."""
    key = str(owner or "")
    with _REGISTRY_GUARD:
        stream: Optional[StateEventStream] = _STREAMS.get(key)
    if stream is not None:
        stream.close()


def stream_names() -> Tuple[str, ...]:
    """Every owner with a live stream. For `service.diagnostics()`."""
    with _REGISTRY_GUARD:
        return tuple(sorted(_STREAMS))


def reset_streams() -> None:
    """Forget every stream. For tests and for shutdown; each one is closed
    first so nothing is left waiting on a stream nobody will publish to."""
    with _REGISTRY_GUARD:
        streams = list(_STREAMS.values())
        _STREAMS.clear()
    for stream in streams:
        try:
            stream.close()
        except Exception as exc:  # noqa: BLE001 - shutdown never raises
            logger.debug("state events: closing %r failed: %s", stream.owner, exc)
