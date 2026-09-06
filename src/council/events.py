"""
council/events.py — the room as a stream a client may leave and come back to.

Two failures shaped this file, and both are already written down in this tree.

The first is in `src/contracts/event.py`: an SSE frame with a NAME on it never
reaches a page's `onmessage`, so a client written against the unnamed dispatch
stream goes silently deaf on a named one — "that cost us a debugging session
once already".  So `sse()` here emits the same unnamed frame and puts the
event's name INSIDE the JSON, which is where a consumer routes on it anyway.

The second is the one section 20 names outright: "reconexión SSE no duplica
eventos".  A council turn is minutes of several models talking; a closed lid, a
proxy timeout or a reload in the middle of one is ordinary.  `since(seq)`
therefore returns strictly what follows `seq`, so a consumer resuming from the
last id it saw gets no repeats — and when the ring buffer has already dropped
what it asks for, it SAYS so with a gap marker rather than quietly handing back
a shorter list.  A silent hole is worse than a warning: the warning costs a
refresh, the hole costs a decision nobody knows was taken.

Three more rules earn their place here:

* **The common payload is not optional** (section 1.8).  `owner`, `project_id`,
  `council_id`, `activity_id`, `session_id`, `run_id`, `actor_id`, `event_id`,
  `causation_id`, `correlation_id` and `at` are on every event, as empty
  strings when nobody supplied them.  A consumer forced to write
  `payload.get("run_id", "")` on every branch eventually forgets a branch, and
  the branch it forgets is the one that carries the effect.

* **No secret leaves in an event.**  The payload goes through the redactor
  `src/contracts/event.py` already owns — `Event.redact()` — and not through a
  third copy of the same regexes (`core/log_safety.py` has the second, for
  text).  `payload["redactions"]` says how many values went, because a field
  that was quietly dropped is indistinguishable from a field that never
  existed, and only one of those is safe to reason from.  It fails CLOSED: a
  payload that could not be scrubbed is a payload that is not published.

* **A late event is kept, marked and inert** (section 20, "un resultado tardío
  tras cancelar no cambia el turno").  After `close()` the stream is closed for
  good: a straggler is still appended and still numbered — the audit has to be
  honest about what arrived — but it carries `late: True`, it wakes no waiter,
  it reopens nothing and no consumer may act on it.

The names are this module's own `COUNCIL_EVENTS` and NOT
`src/contracts/event.py::EVENT_NAMES`.  That tuple is closed on purpose and
belongs to the envelope contract; these fifteen names should be added to it in
a one-line diff with a reason, the way `project_context_*` was, and until they
are, nothing here writes into it.  A name this module does not declare is
published as `council_error` carrying `unknown_event`, never dropped and never
raised: an unroutable name in a stream is a string nothing listens to, and a
turn must not die of a typo in a log line.

THREADING.  The buffer is guarded by a `threading.RLock`, so `publish()` is
safe from any thread.  Waiters are `asyncio.Event`s remembered together with
the loop they were made in, and woken through `call_soon_threadsafe` when the
publisher is on another thread — `asyncio.Event.set()` is not thread-safe, and
a wake that silently did nothing would turn every long poll into a full
timeout.  `wait()` sleeps on that event: there is no poll tick in this file.
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

from .contracts import new_id

logger = logging.getLogger(__name__)

__all__ = [
    "COUNCIL_EVENTS",
    "COMMON_PAYLOAD_KEYS",
    "DEFAULT_CAPACITY",
    "CouncilEvent",
    "CouncilEventStream",
    "stream_for",
    "close_stream",
    "reset_streams",
]


#: Section 1.8's list, plus the four a live room cannot do without: the message
#: itself, the turn's state, what it cost and what went wrong.  Closed for the
#: same reason `EVENT_NAMES` is closed: a name nothing routes on is a string in
#: a log, and a name in this tuple is something a page, a hook and an audit all
#: understand.
COUNCIL_EVENTS: Tuple[str, ...] = (
    "council_activity_started",
    "council_participant_resolved",
    "council_context_compiled",
    "council_claim_acquired",
    "council_claim_released",
    "council_objection_recorded",
    "council_task_handed_off",
    "council_decision_recorded",
    "council_activity_verified",
    "council_activity_completed",
    "council_activity_blocked",
    "council_message",
    "council_turn_state",
    "council_usage",
    "council_error",
    # The ledger's own vocabulary.  `CouncilLedger._emit` publishes through
    # this stream, so every name it can emit has to be declared here or the
    # page would receive `council_error` for a transition that went perfectly.
    # They are separate names rather than one `council_ledger` umbrella because
    # a client that has to open a payload to find out whether a claim was taken
    # or refused is a client that will get it wrong once.
    "council_task_added",
    "council_task_assigned",
    "council_task_status",
    "council_claim_conflicted",
    "council_claim_handoff_refused",
    "council_claim_transferred",
    "council_objection_resolved",
    "council_decision_superseded",
)

#: Section 1.8's "payload común", with the timestamp under the spelling
#: `src/contracts/event.py` already uses (`at`).  Present on every event as
#: `""` when unsupplied — absent and empty are two different facts, and a
#: consumer should only ever have to handle one of them.
COMMON_PAYLOAD_KEYS: Tuple[str, ...] = (
    "owner", "project_id", "council_id", "activity_id", "session_id",
    "run_id", "actor_id", "event_id", "causation_id", "correlation_id", "at",
)

#: ~2000 events is a long council turn.  Past it the oldest are dropped and
#: `since()` announces the hole.
DEFAULT_CAPACITY = 2000

#: The most events one `since()` will hand back, whatever the caller asks for.
MAX_LIMIT = 1000
DEFAULT_LIMIT = 200

_ERROR_EVENT = "council_error"


def _redact(payload: Mapping[str, Any]) -> Tuple[Dict[str, Any], int]:
    """Scrub the payload with the envelope's own redactor.

    `src/contracts/event.py` owns the only structural redactor in this path;
    `Event` is built here purely to borrow `redact()`, which walks a payload
    blanking secret-looking keys and counting what it blanked.  Writing a third
    one is how two of them drift apart, and the one that drifts is always the
    one nobody tested.

    Fails CLOSED.  If the redactor cannot be reached or throws, the event keeps
    its envelope and loses its payload: a payload we could not scrub is a
    payload we do not publish.
    """
    try:
        from src.contracts.event import Event
        scrubbed = Event(name="council", data=dict(payload)).redact(())
        return dict(scrubbed.data), int(scrubbed.redactions)
    except Exception as e:  # noqa: BLE001 - never raise on the turn path
        logger.warning("council events: redaction unavailable, payload dropped: %s", e)
        return {"redaction_failed": True}, 0


def _with_common(payload: Dict[str, Any], *, session_id: str, event_id: str,
                 created_at: str, late: bool, redactions: int) -> Dict[str, Any]:
    """Fill section 1.8's common keys in place and return the payload.

    `session_id` and `event_id` come from the stream and from the event, never
    from the caller: a publisher that could name someone else's session could
    put an event in someone else's room.
    """
    payload["event_id"] = event_id
    payload["session_id"] = session_id
    payload["at"] = str(payload.get("at") or created_at)
    for key in COMMON_PAYLOAD_KEYS:
        value = payload.get(key, "")
        payload[key] = "" if value is None else value
    payload["late"] = bool(late)
    payload["redactions"] = int(redactions)
    return payload


@dataclass(frozen=True)
class CouncilEvent:
    """One thing that happened in a room, numbered so it can be resumed from.

    `seq` is monotonic within a session and is the resume cursor: a client
    sends back the last one it saw and gets strictly what followed.  `id` is a
    separate value on purpose — it identifies the event across sessions and in
    a ledger row, while `seq` only means anything inside its own stream.

    `causation_id` (what caused this) and `correlation_id` (what larger thing
    this belongs to) are fields rather than payload keys because every trace
    across Context Ledger, State Mirror and the immune system joins on them.
    """

    id: str
    seq: int
    name: str
    session_id: str
    activity_id: str
    payload: Mapping[str, Any]
    created_at: str
    causation_id: str = ""
    correlation_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "seq": self.seq,
            "name": self.name,
            "session_id": self.session_id,
            "activity_id": self.activity_id,
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
        raising anything a developer could see.  The council stream is read by
        the same kind of client, so it speaks the same dialect.

        `default=str` because a payload that will not serialise must cost its
        own frame, never the whole stream.
        """
        try:
            body = json.dumps(self.to_dict(), ensure_ascii=False,
                              sort_keys=True, default=str)
        except Exception as e:  # noqa: BLE001 - one bad frame, not a dead stream
            logger.warning("council events: %s (%s) would not serialise: %s",
                           self.id, self.name, e)
            body = json.dumps({
                "id": self.id, "seq": self.seq, "name": _ERROR_EVENT,
                "session_id": self.session_id, "activity_id": self.activity_id,
                "payload": {"unserializable": True}, "created_at": self.created_at,
                "causation_id": self.causation_id,
                "correlation_id": self.correlation_id,
            }, sort_keys=True)
        return "data: " + body + "\n\n"


class CouncilEventStream:
    """One session's ordered, resumable, redacted event log.

    In memory and bounded.  The durable half of section 14 ("los eventos de
    decisión, efectos, claims, aprobación, prueba y consumo son duraderos")
    belongs to `persistence.py`; this is the live stream a page reads, and the
    two are deliberately not the same object — a ring buffer that also had to
    be a source of truth would have to choose between forgetting and growing.
    """

    def __init__(self, session_id: str, *, capacity: int = DEFAULT_CAPACITY) -> None:
        self.session_id = str(session_id or "")
        self.capacity = max(1, int(capacity or DEFAULT_CAPACITY))
        self._events: Deque[CouncilEvent] = deque(maxlen=self.capacity)
        self._guard = threading.RLock()
        self._waiters: List[Tuple[asyncio.Event, Any]] = []
        self._seq = 0
        self._dropped = 0
        self._late = 0
        self._closed = False

    # ── state ──────────────────────────────────────────────────────────────

    @property
    def closed(self) -> bool:
        with self._guard:
            return self._closed

    def last_seq(self) -> int:
        """The highest sequence number this session has issued.

        Monotonic for the life of the stream, and the stream outlives its own
        `close()` in the registry, so a client that reconnects after the room
        ended still resumes from a number that means what it meant before.
        """
        with self._guard:
            return self._seq

    def stats(self) -> Dict[str, Any]:
        with self._guard:
            return {
                "session_id": self.session_id,
                "capacity": self.capacity,
                "buffered": len(self._events),
                "last_seq": self._seq,
                "dropped": self._dropped,
                "late": self._late,
                "closed": self._closed,
                "waiters": len(self._waiters),
            }

    # ── publishing ─────────────────────────────────────────────────────────

    def publish(self, name: str, **payload: Any) -> CouncilEvent:
        """Number, redact, store and broadcast one event.  Never raises.

        An unknown name becomes `council_error` carrying `unknown_event`: the
        stream's vocabulary stays closed, so nothing downstream has to route on
        a name it has never heard of, and the information is still there for
        whoever has to find the typo.

        After `close()` the event is still numbered and still stored, but it is
        marked `late` and wakes nobody — see the module docstring.
        """
        raw = str(name or "").strip()
        known = raw in COUNCIL_EVENTS
        body = dict(payload)
        if not known:
            logger.warning(
                "council events %s: %r is not a council event name; published as "
                "%s so that nothing routes on a name this module never declared",
                self.session_id, raw, _ERROR_EVENT)
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
            logger.info("council events %s: %s arrived after close and is kept, "
                        "marked late, and acted on by nothing", self.session_id, raw)
            return event
        self._wake()
        return event

    def _make(self, seq: int, name: str, data: Dict[str, Any],
              redactions: int, late: bool) -> CouncilEvent:
        event_id = new_id("cev")
        created_at = now_iso()
        payload = _with_common(dict(data), session_id=self.session_id,
                               event_id=event_id, created_at=created_at,
                               late=late, redactions=redactions)
        return CouncilEvent(
            id=event_id, seq=seq, name=name, session_id=self.session_id,
            activity_id=str(payload.get("activity_id") or ""),
            payload=payload, created_at=created_at,
            causation_id=str(payload.get("causation_id") or ""),
            correlation_id=str(payload.get("correlation_id") or ""),
        )


    # ── reading ────────────────────────────────────────────────────────────

    def since(self, seq: int, *, limit: int = DEFAULT_LIMIT) -> List[CouncilEvent]:
        """Strictly what follows `seq`, oldest first, at most `limit` of them.

        "Strictly" is the whole guarantee (section 20): a client that reconnects
        with the last id it saw receives neither that event again nor anything
        before it.  A client that is somehow ahead of the stream receives
        nothing rather than a rewind.

        When the buffer has already dropped what the caller asks for, the list
        STARTS with a `council_error` gap marker naming the range that is gone.
        The marker is built, not published: it consumes no sequence number and
        is not stored, so two clients resuming from the same place see the same
        stream and neither of them shifts it.
        """
        try:
            after = int(seq)
        except (TypeError, ValueError):
            logger.debug("council events %s: unreadable cursor %r, treated as 0",
                         self.session_id, seq)
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
                logger.info("council events %s: consumer resumed at %d but the "
                            "buffer starts at %d; %d event(s) are gone",
                            self.session_id, after, oldest, missed)
                out.insert(0, self._gap(after, oldest, missed))
        return out[:cap]

    def _gap(self, after: int, oldest: int, missed: int) -> CouncilEvent:
        """The marker that says a hole is a hole.

        Its `seq` is `oldest - 1`, so a consumer that stores the last seq it
        saw lands exactly where the surviving events begin instead of asking
        for the missing ones again on every reconnect.
        """
        event_id = new_id("cev")
        created_at = now_iso()
        payload = _with_common(
            {"gap": True, "missed": int(missed), "from_seq": after + 1,
             "to_seq": oldest - 1, "capacity": self.capacity,
             "reason": "buffer_overflow"},
            session_id=self.session_id, event_id=event_id,
            created_at=created_at, late=False, redactions=0)
        return CouncilEvent(id=event_id, seq=max(0, oldest - 1),
                            name=_ERROR_EVENT, session_id=self.session_id,
                            activity_id="", payload=payload,
                            created_at=created_at)

    async def wait(self, seq: int, *, timeout_s: float = 25.0) -> List[CouncilEvent]:
        """Long poll: return as soon as there is anything after `seq`, or `[]`
        when the deadline passes.

        Sleeps on an `asyncio.Event` that `publish()` sets — there is no poll
        tick and no `sleep(0.1)` loop, the same design `src/dispatch.py` uses
        for its condition waits.  A timeout is not an error: it answers with an
        empty list, and the caller reconnects with the cursor it already had.

        A closed stream answers immediately with whatever is left, so a page
        that was long-polling when the room ended is not held for the full
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


    # ── waking the long polls ──────────────────────────────────────────────

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
        another one it can be lost, and a lost wake turns a long poll into a
        full-length timeout that looks exactly like a stalled room.
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
            except Exception as e:  # noqa: BLE001 - a wake never breaks a publish
                logger.debug("council events %s: waking a waiter failed: %s",
                             self.session_id, e)

    def close(self) -> None:
        """End the stream.  Terminal: nothing reopens it.

        Every waiter is woken so no long poll is left hanging for its full
        timeout, and every event published afterwards is kept, numbered and
        marked `late` — the room is over, but what arrived after it is part of
        the record of what happened.
        """
        with self._guard:
            if self._closed:
                return
            self._closed = True
        logger.debug("council events %s: closed at seq %d", self.session_id, self.last_seq())
        self._wake()


# ── one stream per room ────────────────────────────────────────────────────
#
# Keyed by session id and kept even after `close()`.  A closed stream that was
# forgotten would be recreated by the next publisher with its sequence back at
# 1, and every reconnecting client would then be handed events it had already
# seen under numbers it had already used — the exact duplication section 20
# forbids.  `reset_streams()` is the only thing that forgets, and it is for
# tests and shutdown.

_STREAMS: Dict[str, CouncilEventStream] = {}
_REGISTRY_GUARD = threading.Lock()


def stream_for(session_id: str) -> CouncilEventStream:
    """The stream for this session, made once and kept."""
    key = str(session_id or "") or "council_unknown"
    with _REGISTRY_GUARD:
        stream = _STREAMS.get(key)
        if stream is None:
            stream = _STREAMS[key] = CouncilEventStream(key)
            logger.debug("council events %s: stream created", key)
        return stream


def close_stream(session_id: str) -> None:
    """Close this session's stream if it has one.  The stream stays in the
    registry so its sequence numbers stay meaningful to a client that
    reconnects after the room ended."""
    key = str(session_id or "") or "council_unknown"
    with _REGISTRY_GUARD:
        stream: Optional[CouncilEventStream] = _STREAMS.get(key)
    if stream is not None:
        stream.close()


def reset_streams() -> None:
    """Forget every stream.  For tests and for shutdown; each one is closed
    first so nothing is left waiting on a stream nobody will publish to."""
    with _REGISTRY_GUARD:
        streams = list(_STREAMS.values())
        _STREAMS.clear()
    for stream in streams:
        try:
            stream.close()
        except Exception as e:  # noqa: BLE001 - shutdown never raises
            logger.debug("council events: closing %s failed: %s",
                         stream.session_id, e)
