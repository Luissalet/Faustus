"""
council/orchestrator.py -- the state machine of one turn, and nothing else.

Four failures are the reason this file exists, and each one is a rule below.

**The event that lied.**  A room that publishes `turn.phase_changed` and then
fails to persist the phase leaves every consumer -- the page, State Mirror, the
context ledger -- ahead of the source of truth, and nothing later notices.  So
the state is written first and the event is emitted second, always, and the
tests assert the ORDER and not merely that both happened (8).

**The retry that paid twice.**  A POST whose response was lost is retried by
every honest client.  Without `idempotency_key` that is a second turn, a second
set of generations and a second bill; with it, the second request is answered
with the first turn's id and nothing runs again (8, 15.3, 20).

**The answer that arrived after the cancel.**  A model call cannot be un-sent.
When its answer lands after the user cancelled, it is kept as a late event --
the record of what happened is not the room's to edit -- and it advances
nothing: not the turn, not a task, not the ledger (8, 20).

**"Ya está arreglado".**  The rule the plan repeats more than any other (1.7,
12.2, 25, 20): a model saying it finished does not make anything finished.
This file therefore never reads a message's CONTENT to decide anything.  A task
moves because an injected executor returned a structured result and the ledger
accepted it, never because a sentence claimed it, and the invoker's result is
read through a fixed key list so that a model returning `{"task_status":
"done"}` changes exactly nothing.

Two things this module deliberately is not:

* It is **not an agent loop** (25).  `ModelInvoker` and `TaskExecutor` are
  injected; the existing streaming agent, Dispatch and Tournament stay where
  they are, and the only thing that calls anything here is the double a test
  hands in.
* It is **not a second permission system**.  It never widens a tool profile,
  never grants a claim by itself and never approves anything on the user's
  behalf; the ledger owns claims, the gate owns effects, `prove` owns verdicts.

`run_turn()` never raises.  A turn that fails ends in `failed` with a stop
reason, an event and a summary, because an exception escaping onto the turn
path is a room that stops answering without ever saying why.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Protocol, Sequence, Tuple

from src.council import synthesis as _synthesis
from src.council.contracts import (
    AUDIENCE_ROOM,
    MESSAGE_TYPES,
    CouncilError,
    CouncilMessage,
    CouncilParticipant,
    CouncilTurn,
    is_terminal_turn,
    new_id,
)
from src.council.policies import SEQUENTIAL, Selection, policy_for

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_INVOKE_TIMEOUT_S",
    "RESERVE_TIMEOUT_S",
    "INVOCATION_KEYS",
    "EXECUTION_KEYS",
    "VERIFIABLE_TASK_STATUSES",
    "TurnOutcome",
    "ModelInvoker",
    "TaskExecutor",
    "CouncilOrchestrator",
    "use_packet_builder",
    "reset_packet_builder",
]


#: How long one participant may take before the room stops waiting for it.  A
#: turn that waits forever on one model is a room the user cannot get back.
DEFAULT_INVOKE_TIMEOUT_S = 120.0

#: How long a participant waits for its turn on the machine.  Past this the
#: scheduler answers `None`, which is a normal outcome of a busy box and is
#: recorded rather than raised (3.6).
RESERVE_TIMEOUT_S = 30.0

#: **The whole of the invoker's contract.**  Anything else in the returned
#: mapping is logged at debug and ignored -- deliberately, and this is rule 8
#: of the module docstring in one line: a model that returns `task_status`,
#: `verified` or `approved` is a model asserting an outcome, and an assertion
#: is not evidence (1.7).  Adding a key here is a decision about what a model
#: is allowed to change, and it should read like one.
INVOCATION_KEYS: Tuple[str, ...] = ("content", "abstained", "error", "usage", "message_type")

#: What an executor's result may say.  Wider than the invoker's, because an
#: executor is Dispatch or a subagent run reporting a *measured* outcome; even
#: so, `status` is applied through `ledger.set_task_status`, which refuses to
#: record `done` over an open blocking objection (12.1).
#:
#: `changes` and `verification` are here because they are the OBSERVATION half
#: of 12.1: `dispatch.compact(job)` reports what Faustus saw on disk and what
#: the run's checks returned, and `adapters.verify_task` builds a ChangeSet
#: from them.  Dropping them -- which this tuple did until it named them -- left
#: the verifier with nothing to reconcile the ledger's claims against, so no
#: task could ever be proved and `verified` was a rung nothing could reach.
#: They are still not a status: `verify_task` decides that, and `prove` decides
#: `verify_task`.
EXECUTION_KEYS: Tuple[str, ...] = ("status", "run_id", "resources", "evidence", "error",
                                   "usage", "changes", "verification")

#: The task statuses a room may try to upgrade to `verified`.  A task that ran
#: and finished is the only kind there is evidence about; a `failed` or
#: `blocked` one is not re-examined, because nothing it left behind is a claim
#: about success.
VERIFIABLE_TASK_STATUSES: Tuple[str, ...] = ("done", "review")

#: `adapters.VERIFIED`, spelled out rather than imported: naming it here keeps
#: `src.council.adapters` -- and behind it Dispatch, `changesets` and `prove` --
#: off the import path of a module a policy test builds a room with.  The two
#: strings are the same on purpose and mean different things: this is the
#: VERDICT the verifier returned...
VERIFIED_VERDICT = "verified"
#: ...and this is the task STATUS the ledger writes when it did.  They are two
#: constants because one of them is a claim about evidence and the other is a
#: claim about a row, and a build that renamed one should not silently rename
#: the other.
VERIFIED_STATUS = "verified"

#: `src/prove.py`'s verdicts, weakest first.  `_proof_packet` walks this order
#: to pick the packet a multi-task close is judged by: a room is only as proved
#: as its least proved task.
WEAKEST_PROOF_VERDICTS: Tuple[str, ...] = ("contradicted", "unproved", "partial")

#: Messages the room reads to build a packet.  Bounded because a council
#: transcript grows with participants x rounds and 10 forbids copying the whole
#: thing into every call.
HISTORY_LIMIT = 400


# -- the packet builder, injectable without widening the constructor --------
#
# `context.build_packet` is the real one and it pulls in the Context Engine.  A
# test that only wants to prove the state machine should not have to compile a
# context packet to do it, and a room whose context compiler is broken should
# degrade rather than stop -- so the builder is a module-level hook with the
# same shape `context.use_compiler` already has in this package.

_PACKET_BUILDER: Optional[Callable[..., Any]] = None
_PACKET_BUILDER_SET = False


def use_packet_builder(builder: Optional[Callable[..., Any]]) -> None:
    """Install the packet builder for this process (tests, and the doctor).

    `None` means "no packets": every participant is invoked with a degraded
    stand-in that names the room and the assignment.  It is not the same as
    leaving the hook unset, which means "use `context.build_packet`"."""
    global _PACKET_BUILDER, _PACKET_BUILDER_SET
    _PACKET_BUILDER = builder
    _PACKET_BUILDER_SET = True


def reset_packet_builder() -> None:
    """Go back to `context.build_packet`."""
    global _PACKET_BUILDER, _PACKET_BUILDER_SET
    _PACKET_BUILDER = None
    _PACKET_BUILDER_SET = False


def _packet_builder() -> Optional[Callable[..., Any]]:
    if _PACKET_BUILDER_SET:
        return _PACKET_BUILDER
    try:
        from src.council.context import build_packet

        return build_packet
    except Exception as exc:  # noqa: BLE001 - never fatal on the turn path
        logger.warning("council orchestrator: no context compiler in this build (%s); "
                       "participants will be invoked with a degraded packet", exc)
        return None


# -- what a turn produced ---------------------------------------------------

@dataclass(frozen=True)
class TurnOutcome:
    """The result of one turn, as a value a route can serialise.

    `summary` is `synthesis.build()`'s object -- built from the LEDGER and not
    from the transcript (12.2) -- or `None` when the close could not be built,
    which is a fact worth carrying rather than an empty summary that looks like
    a clean finish.

    `blocked_on` is the sentence a person reads: which limit, which objection,
    which pause.  `stop_reason` is the machine-readable half from
    `contracts.STOP_REASONS`.  Two fields because a reason a policy routes on
    must come from a closed vocabulary and a reason a person reads must contain
    the numbers (12.3).
    """

    turn_id: str
    state: str
    stop_reason: str
    messages: Tuple[str, ...]
    summary: Optional[Any]
    blocked_on: str = ""

    def to_dict(self) -> Dict[str, Any]:
        summary = self.summary
        to_dict = getattr(summary, "to_dict", None)
        return {
            "turn_id": self.turn_id,
            "state": self.state,
            "stop_reason": self.stop_reason,
            "messages": list(self.messages),
            "summary": to_dict() if callable(to_dict) else summary,
            "blocked_on": self.blocked_on,
        }


class ModelInvoker(Protocol):
    """One call to one participant.  Implemented outside this package (25).

    It returns a mapping, and only `INVOCATION_KEYS` of it are read.  It is not
    given the ledger, the store or the scheduler: a participant's answer is
    text and usage, and everything it might want to CHANGE goes through the
    orchestrator, the ledger and the gate, in that order.
    """

    async def invoke(self, *, participant: Any, packet: Any, turn: Any,
                     timeout_s: float) -> Dict[str, Any]:
        ...


class TaskExecutor(Protocol):
    """One delegated task.  Dispatch, a subagent run, or a test double.

    Only `EXECUTION_KEYS` are read, and `status` still has to survive
    `ledger.set_task_status` -- an executor reporting `done` over an open
    blocking objection is refused exactly like anybody else (12.1).
    """

    async def run(self, *, task: Any, participant: Any, session: Any) -> Dict[str, Any]:
        ...


# -- small readers ----------------------------------------------------------

def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _get(obj: Any, name: str, default: Any = "") -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _known(result: Any, keys: Sequence[str], *, label: str) -> Dict[str, Any]:
    """The declared keys of a result, and a debug line naming the rest.

    This is where "a model's claim changes nothing" is actually enforced.  An
    unknown key is not an error -- an invoker from a newer build may carry
    fields this one does not read -- but it is never acted on, and it is logged
    so that a field somebody expected to work is findable instead of silent.
    """
    if not isinstance(result, Mapping):
        if result is None:
            return {}
        logger.warning("council orchestrator: %s returned %s, not a mapping; ignored",
                       label, type(result).__name__)
        return {}
    extra = sorted(set(result) - set(keys))
    if extra:
        logger.debug("council orchestrator: %s returned %s, which this build does not read; "
                     "an outcome is asserted by evidence, never by a returned field", label, extra)
    return {key: result[key] for key in keys if key in result}


async def _maybe_await(value: Any) -> Any:
    """Accept a coroutine or a plain value.

    The real packet builder is a coroutine and a test double usually is not;
    making every double async would be a rule people forget on the one call
    site that matters."""
    if inspect.isawaitable(value):
        return await value
    return value


def _tokens(usage: Any) -> int:
    """What one call cost, from whichever field the endpoint filled in.

    The order matters and the sum at the end matters more.  A reported total is
    the endpoint's own arithmetic and is trusted first; failing that, prompt and
    completion are ADDED, because they are two halves of one number.  Falling
    through to `output_tokens` alone -- which is what this did until the close
    started printing the split and showed `1228/79/79` on a real turn -- charges
    a budget for the answer and not for the question, and prints a total
    smaller than one of its own parts.
    """
    if not isinstance(usage, Mapping):
        return 0

    def read(key: str) -> int:
        try:
            return max(0, int(usage.get(key) or 0))
        except (TypeError, ValueError):
            return 0

    for key in ("total_tokens", "tokens"):
        value = read(key)
        if value:
            return value
    both = (read("input_tokens") or read("prompt_tokens")) \
        + (read("output_tokens") or read("completion_tokens"))
    return both


# -- the orchestrator -------------------------------------------------------

class CouncilOrchestrator:
    """One room's turns, from `received` to a terminal state.

    Everything it depends on is injected, and every default is a lazy lookup of
    the module that already owns that concern: the store for state, the ledger
    for tasks/claims/objections/decisions, the scheduler for the machine and
    the budget, the event stream for the page.  Nothing here re-implements any
    of them (6.3, 25).

    It is not thread-safe and does not need to be: one room's turns run on one
    event loop, and the commands (`pause`, `cancel_turn`, ...) serialise on an
    `asyncio.Lock` so two of them cannot interleave halfway through a write.
    Concurrency BETWEEN processes is handled where it has to be -- the store's
    `expected_revision` and its unique idempotency index (8, 15.3).
    """

    def __init__(self, session: Any, *, store: Any = None, ledger: Any = None,
                 scheduler: Any = None, events: Any = None, invoker: Any = None,
                 executor: Any = None, verifier: Any = None,
                 clock: Optional[Callable[[], float]] = None) -> None:
        self.session = session
        self.session_id = _text(_get(session, "id", ""))
        self.owner = _text(_get(session, "owner", ""))
        self.workspace = _text(_get(session, "workspace", ""))
        self._store = store if store is not None else _default_store()
        self._ledger = ledger if ledger is not None else _default_ledger(
            self.session_id, self.workspace)
        self._scheduler = scheduler if scheduler is not None else _default_scheduler(session)
        self._events = events if events is not None else _default_events(self.session_id)
        self._invoker = invoker
        self._executor = executor
        #: `adapters.verify_task`, or a double.  Resolved lazily by
        #: `_verify_task` so a room can be built without importing Dispatch,
        #: `changesets` and `prove` -- the same reason every other engine here
        #: is a lazy default (6.3, 25).
        self._verifier = verifier
        #: task_id -> the proof packet `verify_task` came back with.  Kept
        #: because `synthesis.status_of` cannot reach `verified` without one,
        #: and the packet is the only thing in the room that is evidence rather
        #: than a report (12.1).
        self._proofs: Dict[str, Dict[str, Any]] = {}
        #: Prompt/completion tokens, summed as each result comes in.  The
        #: scheduler counts a single total because a budget only has to know
        #: how much is left; a close has to say what it spent it ON, and
        #: `synthesis._usage` has had `input_tokens`/`output_tokens` fields
        #: waiting for a source since it was written.
        self._io_tokens: Dict[str, int] = {"input_tokens": 0, "output_tokens": 0}
        self._clock: Callable[[], float] = clock or time.monotonic
        self._started = float(self._clock())
        # ONE stream per room.  Without this the ledger resolves the module
        # registry's stream while the orchestrator publishes to whatever it was
        # given, and a room built around an injected stream silently loses every
        # claim, objection and decision event.
        try:
            self._ledger.publish_to(self._events)
        except AttributeError:
            logger.debug("council orchestrator %s: this ledger does not publish events",
                         self.session_id)
        except Exception as exc:  # noqa: BLE001 - never fatal on construction
            logger.warning("council orchestrator %s: the ledger's stream could not be "
                           "pointed at this room's: %s", self.session_id, exc)

        #: The revision this orchestrator last saw.  Every session write states
        #: it, so a second contradictory order gets a conflict instead of
        #: overwriting the first (8).
        self._revision = self._read_revision()
        self._paused = False
        self._cancelled: set = set()
        self._stopped: set = set()
        #: participant -> the resources a non-cancellable tool is writing right
        #: now.  `cancel` reads it and does NOT release those claims (20:
        #: "cancelar no libera prematuramente un recurso en uso").
        self._mutating: Dict[str, Tuple[str, ...]] = {}
        #: participant -> the task whose executor is running.  Separate from
        #: `_mutating` because a task can be running before it has claimed
        #: anything, and a handoff must be refused then too: the gate in 11.3
        #: is "is a mutating tool active", not "does it hold a file yet".
        self._running_tasks: Dict[str, str] = {}
        #: Answers that arrived after their turn was cancelled.  Kept, counted,
        #: and acted on by nothing.
        self._late: List[Dict[str, Any]] = []
        self._lock: Optional[asyncio.Lock] = None

    # -- plumbing ----------------------------------------------------------

    def _guard(self) -> asyncio.Lock:
        """The command lock, built on first use inside the running loop."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _read_revision(self) -> int:
        try:
            return max(1, int(_get(self.session, "revision", 1) or 1))
        except (TypeError, ValueError):
            return 1

    def _policy_name(self) -> str:
        return _text(_get(self.session, "policy", "")) or "chat"

    def _publish(self, name: str, **payload: Any) -> None:
        """Emit one event.  Called only AFTER the state it describes is written.

        Never raises: an event stream that is full, closed or broken must not be
        able to fail a turn whose state is already durable."""
        try:
            self._events.publish(name, council_id=self.session_id, owner=self.owner, **payload)
        except Exception as exc:  # noqa: BLE001 - the page is not the room
            logger.warning("council orchestrator %s: publishing %s failed: %s",
                           self.session_id, name, exc)

    def _participants(self) -> List[Any]:
        """The room's seats, from the store, falling back to the session.

        The fallback is a list of ids, which every policy accepts: a room whose
        participant rows cannot be read can still route a mention, and that is
        better than a turn that cannot start."""
        try:
            seats = list(self._store.list_participants(self.session_id) or ())
            if seats:
                return seats
        except Exception as exc:  # noqa: BLE001 - read path
            logger.warning("council orchestrator %s: participants unreadable (%s); "
                           "falling back to the session's id list", self.session_id, exc)
        return [str(pid) for pid in (_get(self.session, "participants", ()) or ())]

    def _seat(self, participants: Sequence[Any], participant_id: str) -> Any:
        for entry in participants or ():
            inner = getattr(entry, "participant", entry)
            if _text(_get(inner, "id", "")) == participant_id:
                return inner
            if isinstance(entry, str) and entry == participant_id:
                return CouncilParticipant(id=participant_id)
        return CouncilParticipant(id=participant_id)

    def _history(self, *, viewer_id: str = "") -> List[Any]:
        try:
            return list(self._store.list_messages(
                self.session_id, viewer_id=viewer_id, limit=HISTORY_LIMIT) or ())
        except Exception as exc:  # noqa: BLE001 - read path
            logger.warning("council orchestrator %s: transcript unreadable: %s",
                           self.session_id, exc)
            return []

    def _spend(self, usage: Any) -> None:
        """Record one result's cost, in both the shapes the room needs.

        The scheduler gets the single total it enforces a budget with; the
        input/output split is kept here, because it is the only place that sees
        every result and `synthesis` has nowhere else to read it from.  A usage
        mapping that carries neither field costs nothing and is not an error:
        plenty of endpoints report only a total, and inventing a split from one
        number would be a measurement this module did not make.
        """
        self._scheduler.spend(tokens=_tokens(usage))
        if not isinstance(usage, Mapping):
            return
        for field_name, keys in (("input_tokens", ("input_tokens", "prompt_tokens")),
                                 ("output_tokens", ("output_tokens", "completion_tokens"))):
            for key in keys:
                try:
                    value = int(usage.get(key) or 0)
                except (TypeError, ValueError):
                    continue
                if value > 0:
                    self._io_tokens[field_name] += value
                    break

    def _usage(self) -> Dict[str, Any]:
        """Consumption, from the scheduler, labelled with where it came from.

        `total_tokens` is the scheduler's, because that is the number a budget
        was enforced against and a close that disagreed with it would be
        describing a different room.  The input/output split is this
        orchestrator's own tally of what the endpoints reported."""
        counted = dict(self._io_tokens)
        try:
            stats = self._scheduler.stats()
            state = dict(stats.get("state") or {})
            return {"total_tokens": int(state.get("total_tokens") or 0),
                    "input_tokens": counted["input_tokens"],
                    "output_tokens": counted["output_tokens"],
                    "calls": int(stats.get("granted") or 0),
                    "wall_seconds": float(state.get("wall_seconds") or 0.0),
                    "waited_ms": int(stats.get("waited_ms_total") or 0),
                    "source": "council_scheduler"}
        except Exception as exc:  # noqa: BLE001 - read path
            logger.debug("council orchestrator %s: usage unreadable: %s", self.session_id, exc)
            return {**counted, "source": "unreported"}


    # -- write the state, then emit the event (8) --------------------------

    def _advance(self, turn: Any, state: str, *, stop_reason: str = "",
                 patch: Optional[Mapping[str, Any]] = None, **payload: Any) -> Any:
        """Move the turn and announce it, **in that order**.

        The order is the rule.  If the process dies between the two, the state
        is durable and the event can be rebuilt from it; the other way round,
        the event is a claim about a transition that never happened, and every
        consumer that trusted it is now describing a room that does not exist
        (8).

        A refused transition is not fatal here: it is logged, the turn is
        returned unchanged, and the caller sees the state it actually has.  The
        graph lives in `contracts.TRANSITIONS` and the store enforces it, so
        this cannot invent an edge even by accident."""
        patched: Dict[str, Any] = {"state": state}
        if stop_reason:
            patched["stop_reason"] = stop_reason
        if patch:
            patched.update(dict(patch))
        try:
            updated = self._store.update_turn(_text(_get(turn, "id", "")), patched)
        except Exception as exc:  # noqa: BLE001 - a refused move is an answer
            logger.warning("council orchestrator %s: turn %s could not move to %r: %s",
                           self.session_id, _text(_get(turn, "id", "")), state, exc)
            self._publish("council_error", turn_id=_text(_get(turn, "id", "")),
                          state=state, error=str(exc), phase="transition")
            return turn
        self._publish("council_turn_state", turn_id=_text(_get(updated, "id", "")),
                      state=state, stop_reason=stop_reason, **payload)
        return updated

    def _commit_message(self, **fields: Any) -> str:
        """Append one message and announce it.  Write first, event second.

        The author is whatever the runtime passed; nothing here reads the text
        to decide who wrote it (3.2).  A message whose first line claims to be
        the user is still stored as what it is, and the surface is told so via
        `claims_identity` rather than the store believing it."""
        message = CouncilMessage.parse({
            "id": new_id("msg"),
            "session_id": self.session_id,
            **{k: v for k, v in fields.items() if v is not None},
        })
        try:
            stored = self._store.append_message(message)
        except Exception as exc:  # noqa: BLE001 - a lost message is not a lost turn
            logger.warning("council orchestrator %s: message could not be stored: %s",
                           self.session_id, exc)
            self._publish("council_error", error=str(exc), phase="message",
                          author_id=message.author_id)
            return ""
        claimed = stored.claims_identity()
        self._publish("council_message", message_id=stored.id, turn_id=stored.turn_id,
                      actor_id=stored.author_id, author_kind=stored.author_kind,
                      message_type=stored.message_type, visibility=stored.visibility,
                      audience=list(stored.audience), committed=True,
                      claims_identity=claimed)
        if claimed:
            logger.info("council orchestrator %s: %s opened its message with %r; the stored "
                        "author is unchanged (3.2)", self.session_id, stored.author_id, claimed)
        return stored.id

    def _summary(self, stop_reason: str) -> Optional[Any]:
        """The close, built from the ledger and never from the transcript (12.2).

        The four arguments are the four things `synthesis.build` cannot reach on
        its own, and each of them is load-bearing:

        * `messages` and `participants` together are what makes a contribution
          line honest.  With the transcript alone, a participant who said
          nothing has no row at all -- and silence in a room that was asked for
          opinions is exactly the thing a reader needs to see (12.2).
        * `proof` is the only route to `verified`.  `status_of` refuses that
          rung without a packet from `prove`, so a close built without this
          argument can never report verification however much was proved.
        * `stop_reason` keeps "it finished" and "it ran out of budget" from
          being the same sentence.
        """
        try:
            return _synthesis.build(self._ledger, messages=self._history(),
                                    participants=self._participants(),
                                    usage=self._usage(), stop_reason=stop_reason,
                                    proof=self._proof_packet())
        except Exception:  # noqa: BLE001 - a close that fails is reported, not raised
            logger.exception("council orchestrator %s: the summary could not be built",
                             self.session_id)
            return None

    # -- receiving a message (13) ------------------------------------------

    async def submit(self, *, author_id: str, content: str, mentions: Sequence[str] = (),
                     idempotency_key: str = "") -> str:
        """Open a turn for one user message and return its id.

        With an `idempotency_key`, a retry of the same request is answered with
        the turn the first one opened: no second turn, no second message, no
        second round of generations (8, 15.3, 20).  The claim is taken BEFORE
        the turn is created, so two processes racing on the same key produce one
        turn and one answer -- the guarantee is the store's unique index, not
        this coroutine's ordering.

        Returns fast and runs nothing: `run_turn()` is a separate call so a
        route can answer with the turn id and let the room work in the
        background (13).
        """
        key = _text(idempotency_key)
        reserved = ""
        if key:
            try:
                fresh, ref = self._store.claim_idempotency(
                    self.session_id, key, kind="turn", ref=new_id("turn"))
            except Exception as exc:  # noqa: BLE001 - a broken claim must not double-charge
                logger.error("council orchestrator %s: idempotency claim failed for %r: %s; "
                             "refusing to open a turn that might be a duplicate",
                             self.session_id, key, exc)
                raise
            if not fresh:
                logger.info("council orchestrator %s: %r already opened turn %s; the retry is "
                            "answered with it", self.session_id, key, ref)
                self._publish("council_turn_state", turn_id=ref, state="received",
                              idempotent_replay=True, idempotency_key=key)
                return ref
            reserved = ref

        turn = CouncilTurn.parse({
            "id": reserved or new_id("turn"),
            "session_id": self.session_id,
            "state": "received",
            "author_id": _text(author_id),
            "content": str(content or ""),
            "policy": self._policy_name(),
            "idempotency_key": key,
        })
        try:
            stored = self._store.create_turn(turn)
        except Exception as exc:  # noqa: BLE001 - DuplicateTurn carries the winner
            existing = _text(getattr(exc, "turn_id", ""))
            if existing:
                logger.info("council orchestrator %s: turn %s already carries %r",
                            self.session_id, existing, key)
                return existing
            # The key is claimed and no turn exists behind it.  That is the
            # conservative side of the trade: a later retry is answered with a
            # turn id that resolves to nothing and fails loudly, which is a
            # better outcome than releasing the key and letting a duplicate
            # request start a second round of paid work (15.3, 25).
            if reserved:
                logger.error("council orchestrator %s: %r is claimed but its turn could not be "
                             "created (%s); a retry will be answered with a turn that does not "
                             "exist rather than opening a second one", self.session_id, key, exc)
            raise
        self._publish("council_turn_state", turn_id=stored.id, state="received",
                      idempotency_key=key)
        audience = tuple(_text(m) for m in (mentions or ()) if _text(m)) or (AUDIENCE_ROOM,)
        self._commit_message(turn_id=stored.id, author_id=_text(author_id), author_kind="user",
                             audience=list(audience), message_type="message",
                             content=str(content or ""), visibility="room")
        self._publish("council_activity_started", turn_id=stored.id, activity_id=stored.id,
                      actor_id=_text(author_id), policy=stored.policy)
        return stored.id


    # -- running a turn (8) ------------------------------------------------

    async def run_turn(self, turn_id: str) -> TurnOutcome:
        """Take one turn from `received` to a terminal state.  Never raises.

        Everything that can go wrong here goes wrong on the path a user is
        waiting on: a model times out, the context compiler throws, the store
        is locked, a policy is misspelt.  An exception escaping this coroutine
        would be a room that stops answering without saying why, so the failure
        is written as `failed`, announced, and returned with a summary that
        says what the room had reached (12.3, 20)."""
        try:
            return await self._run_turn(_text(turn_id))
        except Exception as exc:  # noqa: BLE001 - this is the promise of this method
            logger.exception("council orchestrator %s: turn %s failed", self.session_id, turn_id)
            return self._failed(_text(turn_id), exc)

    def _failed(self, turn_id: str, exc: BaseException) -> TurnOutcome:
        reason = f"{type(exc).__name__}: {exc}"
        turn = None
        try:
            turn = self._store.get_turn(turn_id)
        except Exception:  # noqa: BLE001 - we are already failing
            logger.debug("council orchestrator %s: turn %s unreadable while failing",
                         self.session_id, turn_id)
        if turn is not None and not is_terminal_turn(_text(_get(turn, "state", ""))):
            self._advance(turn, "failed", stop_reason="failed", error=reason)
        self._publish("council_error", turn_id=turn_id, error=reason, phase="run_turn")
        return TurnOutcome(turn_id=turn_id, state="failed", stop_reason="failed",
                           messages=(), summary=self._summary("failed"), blocked_on=reason)

    def _stop_now(self, turn_id: str) -> str:
        """Why the round must not start another call, or `""`.

        Checked before EVERY participant and not only at the top of the turn:
        `pause` and `cancel` arrive while the round is running, and a check that
        only happens once is a pause that takes effect next turn (8)."""
        if turn_id in self._cancelled:
            return "cancelled"
        if self._paused:
            return "paused"
        allowed, why = self._scheduler.can_start()
        if not allowed:
            return why or "budget"
        return ""

    async def _run_turn(self, turn_id: str) -> TurnOutcome:
        turn = self._store.get_turn(turn_id)
        if turn is None:
            self._publish("council_error", turn_id=turn_id, error="no such turn in this room",
                          phase="run_turn")
            return TurnOutcome(turn_id=turn_id, state="failed", stop_reason="failed",
                               messages=(), summary=None,
                               blocked_on=f"turn {turn_id!r} does not exist in this room")
        state = _text(_get(turn, "state", ""))
        if turn_id in self._cancelled or state == "cancelled":
            return TurnOutcome(turn_id=turn_id, state="cancelled", stop_reason="user_stopped",
                               messages=(), summary=None, blocked_on="the turn was cancelled")
        if is_terminal_turn(state):
            return TurnOutcome(turn_id=turn_id, state=state,
                               stop_reason=_text(_get(turn, "stop_reason", "")),
                               messages=(), summary=None,
                               blocked_on=f"the turn was already {state}")

        participants = self._participants()
        policy = policy_for(self._policy_name())

        turn = self._advance(turn, "classified", policy=policy.name)
        selection = policy.select(session=self.session, participants=participants,
                                  message=turn, ledger=self._ledger, history=self._history())
        turn = self._advance(turn, "agenda_ready", phase=selection.phase, mode=selection.mode)
        turn = self._advance(
            turn, "participants_selected",
            patch={"selected_participants": list(selection.participant_ids)},
            participants=list(selection.participant_ids), mode=selection.mode,
            blindness=selection.blindness, reason=selection.reason)
        self._publish("council_participant_resolved",
                      turn_id=turn_id, participants=list(selection.participant_ids),
                      phase=selection.phase, mode=selection.mode,
                      blindness=selection.blindness, reason=selection.reason)

        blocker = self._stop_now(turn_id)
        if blocker:
            return self._blocked(turn, blocker)
        if not selection.participant_ids:
            return self._blocked(turn, "the policy selected nobody for this message")

        self._scheduler.spend(turns=1, rounds=1)
        turn = self._advance(turn, "running")
        turn = self._advance(turn, "speaking", participants=list(selection.participant_ids))
        committed = await self._round(turn, selection, participants)

        if turn_id in self._cancelled:
            return self._cancelled_outcome(turn_id, committed)
        if self._paused:
            turn = self._advance(turn, "running")
            return self._blocked(turn, "paused", messages=committed)

        if self._executor is not None and policy.name in ("collaborate", "pair"):
            turn = self._advance(turn, "running")
            turn = self._advance(turn, "delegating")
            await self._delegate(turn, selection, participants)
            # Checked again: a cancel that arrived while a task was running has
            # already written `cancelled`, and moving on to `reviewing` would be
            # this coroutine arguing with the state it is supposed to serve.
            if turn_id in self._cancelled:
                return self._cancelled_outcome(turn_id, committed)
            if self._paused:
                return self._blocked(self._store.get_turn(turn_id) or turn, "paused",
                                     messages=committed)
            turn = self._advance(turn, "reviewing")

        stop_reason = self._scheduler.stop_reason() or "completed"
        turn = self._advance(turn, "synthesizing")
        summary = self._summary(stop_reason)
        turn = self._advance(turn, "completed", stop_reason=stop_reason)
        self._publish("council_activity_completed", turn_id=turn_id, activity_id=turn_id,
                      stop_reason=stop_reason,
                      status=_text(_get(summary, "status", "")) if summary else "")
        return TurnOutcome(turn_id=turn_id, state=_text(_get(turn, "state", "completed")),
                           stop_reason=stop_reason, messages=tuple(committed), summary=summary)

    def _cancelled_outcome(self, turn_id: str, messages: Sequence[str]) -> TurnOutcome:
        """The turn was cancelled while it was running.

        No transition is written here: `cancel_turn` already wrote `cancelled`
        before anybody was told, and a second write from this side would be the
        round re-deciding a state a person had already settled."""
        return TurnOutcome(turn_id=turn_id, state="cancelled", stop_reason="user_stopped",
                           messages=tuple(messages), summary=None,
                           blocked_on="the turn was cancelled while it was running")

    def _blocked(self, turn: Any, why: str,
                 messages: Sequence[str] = ()) -> TurnOutcome:
        """Stop this turn and say who owes the room something.

        `blocked` is not a dead room (7.1): it means a person, a budget or an
        objection has to move before the turn can go on, and the session itself
        keeps its own status."""
        turn_id = _text(_get(turn, "id", ""))
        reason = self._scheduler.stop_reason() if why not in ("paused", "cancelled") else ""
        stop_reason = reason or ("user_stopped" if why == "paused" else "blocked")
        moved = self._advance(turn, "blocked", stop_reason=stop_reason, blocked_on=why)
        self._publish("council_activity_blocked", turn_id=turn_id, activity_id=turn_id,
                      reason=why, stop_reason=stop_reason)
        return TurnOutcome(turn_id=turn_id, state=_text(_get(moved, "state", "blocked")),
                           stop_reason=stop_reason, messages=tuple(messages),
                           summary=self._summary(stop_reason), blocked_on=why)


    # -- one round ---------------------------------------------------------

    async def _round(self, turn: Any, selection: Selection,
                     participants: Sequence[Any]) -> List[str]:
        """Ask the selected participants, in the mode the policy chose.

        The difference between the modes is what each participant is TOLD, and
        it is enforced here rather than by remembering to do it:

        * `sequential` re-reads the transcript before each call, so a later
          speaker sees the earlier ones' messages -- and only the ones that were
          committed, never a half-streamed answer (20);
        * `parallel` and `blind_parallel` share ONE snapshot taken before the
          round, so no participant can see a peer's answer from this round.
          For `blind_parallel` that is the whole point of the round (4.2); for
          `parallel` it is simply true, since they run at the same time.
        """
        turn_id = _text(_get(turn, "id", ""))
        seats = list(selection.participant_ids)
        if selection.mode == SEQUENTIAL:
            out: List[str] = []
            for participant_id in seats:
                blocker = self._stop_now(turn_id)
                if blocker:
                    logger.info("council orchestrator %s: round stopped before %s (%s)",
                                self.session_id, participant_id, blocker)
                    break
                message_id = await self._speak(turn, participant_id, participants, selection,
                                               history=self._history(viewer_id=participant_id))
                if message_id:
                    out.append(message_id)
            return out

        blocker = self._stop_now(turn_id)
        if blocker:
            return []
        frozen = self._history()
        results = await asyncio.gather(
            *[self._speak(turn, participant_id, participants, selection, history=frozen)
              for participant_id in seats],
            return_exceptions=True)
        out = []
        for participant_id, result in zip(seats, results):
            if isinstance(result, BaseException):
                logger.warning("council orchestrator %s: %s failed in a parallel round: %s",
                               self.session_id, participant_id, result)
                self._publish("council_error", turn_id=turn_id, actor_id=participant_id,
                              error=str(result), phase="invoke")
                continue
            if result:
                out.append(result)
        return out

    async def _speak(self, turn: Any, participant_id: str, participants: Sequence[Any],
                     selection: Selection, *, history: Sequence[Any]) -> str:
        """One participant's intervention, or `""`.

        `""` is a legitimate outcome and never an error: a stopped participant,
        a machine too busy to give it a slot, an abstention with nothing to add
        (20: "una abstención no cuenta como fallo").  Each case is announced
        with its own reason so the room can tell them apart afterwards."""
        turn_id = _text(_get(turn, "id", ""))
        if participant_id in self._stopped:
            self._publish("council_activity_blocked", turn_id=turn_id, actor_id=participant_id,
                          reason="this participant was stopped by the user")
            return ""
        if self._invoker is None:
            logger.error("council orchestrator %s: no ModelInvoker was injected; this module "
                         "does not create one (25)", self.session_id)
            self._publish("council_error", turn_id=turn_id, actor_id=participant_id,
                          error="no model invoker is wired into this room", phase="invoke")
            return ""

        seat = self._seat(participants, participant_id)
        model = _text(_get(seat, "model", ""))
        reservation = await self._scheduler.reserve(participant_id, model,
                                                    timeout_s=RESERVE_TIMEOUT_S)
        if reservation is None:
            allowed, why = self._scheduler.can_start()
            reason = why if not allowed else f"no slot for {model or 'this model'} in time"
            self._publish("council_activity_blocked", turn_id=turn_id, actor_id=participant_id,
                          reason=reason)
            return ""

        try:
            packet = await self._packet(turn, seat, history, selection)
            self._publish("council_context_compiled", turn_id=turn_id, actor_id=participant_id,
                          blindness=selection.blindness, phase=selection.phase,
                          packet_id=_text(_get(packet, "packet_id", "")))
            raw = await self._invoker.invoke(participant=seat, packet=packet, turn=turn,
                                             timeout_s=DEFAULT_INVOKE_TIMEOUT_S)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one participant costs itself
            logger.warning("council orchestrator %s: invoking %s failed: %s",
                           self.session_id, participant_id, exc)
            self._publish("council_error", turn_id=turn_id, actor_id=participant_id,
                          error=f"{type(exc).__name__}: {exc}", phase="invoke")
            return ""
        finally:
            await self._scheduler.release(reservation)

        result = _known(raw, INVOCATION_KEYS, label=f"invoker for {participant_id}")
        self._spend(result.get("usage"))

        if _text(result.get("error")):
            self._publish("council_error", turn_id=turn_id, actor_id=participant_id,
                          error=_text(result.get("error")), phase="invoke")
            return ""

        content = str(result.get("content") or "")
        abstained = bool(result.get("abstained"))
        kind = _text(result.get("message_type"))
        message_type = kind if kind in MESSAGE_TYPES else ("abstention" if abstained else "message")

        # An answer that arrives after the cancel is KEPT and changes nothing
        # (8, 20).  It is not committed to the transcript, because the room
        # already closed this turn and a message appearing after the close would
        # make the transcript disagree with the state; it is published as a late
        # event and counted, so the record of what happened stays complete.
        if turn_id in self._cancelled:
            late = {"turn_id": turn_id, "participant_id": participant_id,
                    "message_type": message_type, "chars": len(content),
                    "content": content}
            self._late.append(late)
            logger.info("council orchestrator %s: %s answered after the turn was cancelled; "
                        "kept as a late event, it advances nothing",
                        self.session_id, participant_id)
            self._publish("council_message", turn_id=turn_id, actor_id=participant_id,
                          message_type=message_type, committed=False, after_cancel=True,
                          chars=len(content))
            return ""

        if abstained and not content.strip():
            self._publish("council_message", turn_id=turn_id, actor_id=participant_id,
                          message_type="abstention", committed=False, abstained=True)
            return ""

        return self._commit_message(
            turn_id=turn_id, author_id=participant_id, author_kind="model",
            audience=[AUDIENCE_ROOM], message_type=message_type, content=content,
            visibility="room",
            metadata={"phase": selection.phase, "mode": selection.mode,
                      "blindness": selection.blindness, "abstained": abstained})

    async def _packet(self, turn: Any, seat: Any, history: Sequence[Any],
                      selection: Selection) -> Any:
        """The context packet for one participant, or a degraded stand-in.

        A compiler that fails costs this participant its context and not the
        room its turn (10): the stand-in still names the room, the assignment
        and the blindness, so a participant is never invoked without stated
        limits."""
        builder = _packet_builder()
        degraded = {"degraded": True, "session_id": self.session_id,
                    "participant_id": _text(_get(seat, "id", "")),
                    "phase": selection.phase, "blindness": selection.blindness,
                    "turn_id": _text(_get(turn, "id", ""))}
        if builder is None:
            return degraded
        try:
            return await _maybe_await(builder(
                session=self.session, participant=seat, ledger=self._ledger,
                messages=list(history), turn=turn, blindness=selection.blindness,
                workspace=self.workspace, owner=self.owner))
        except Exception as exc:  # noqa: BLE001 - context never fails a turn
            logger.exception("council orchestrator %s: context for %s could not be built",
                             self.session_id, _text(_get(seat, "id", "")))
            return {**degraded, "error": f"{type(exc).__name__}: {exc}"}


    # -- delegated work (4.4, 17.3) ----------------------------------------

    async def _delegate(self, turn: Any, selection: Selection,
                        participants: Sequence[Any]) -> None:
        """Run the ready tasks owned by the participants of this round.

        The executor is Dispatch, a subagent run or a double; this coroutine
        only decides WHICH task, records what the run reported, and offers the
        status to the ledger.  It never marks a task done because a message
        said so, and the ledger still refuses `done` over an open blocking
        objection (1.7, 12.1).

        While a task is running, its claimed resources are recorded in
        `self._mutating`, which is what makes `cancel_turn` keep -- rather than
        release -- a claim on a file somebody is halfway through writing (20).
        """
        turn_id = _text(_get(turn, "id", ""))
        try:
            tasks = list(self._ledger.ready_tasks() or ())
        except Exception as exc:  # noqa: BLE001 - read path
            logger.warning("council orchestrator %s: ready tasks unreadable: %s",
                           self.session_id, exc)
            return
        for task in tasks:
            owner = _text(_get(task, "owner_participant_id", ""))
            if owner not in selection.participant_ids:
                continue
            blocker = self._stop_now(turn_id)
            if blocker:
                logger.info("council orchestrator %s: delegation stopped before %s (%s)",
                            self.session_id, _text(_get(task, "id", "")), blocker)
                return
            await self._run_task(turn_id, task, owner, participants)

    async def _run_task(self, turn_id: str, task: Any, owner: str,
                        participants: Sequence[Any]) -> None:
        task_id = _text(_get(task, "id", ""))
        resources = tuple(_text(r) for r in (_get(task, "claimed_resources", ()) or ()) if _text(r))
        self._mutating[owner] = resources
        self._running_tasks[owner] = task_id
        try:
            raw = await self._executor.run(task=task, participant=self._seat(participants, owner),
                                           session=self.session)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one task costs itself
            logger.warning("council orchestrator %s: task %s failed to run: %s",
                           self.session_id, task_id, exc)
            self._publish("council_error", turn_id=turn_id, task_id=task_id, actor_id=owner,
                          error=f"{type(exc).__name__}: {exc}", phase="execute")
            return
        finally:
            # Cleared even on failure: a resource nobody is writing any more
            # must not stay pinned as "in use" and block every later cancel.
            self._mutating.pop(owner, None)
            self._running_tasks.pop(owner, None)

        result = _known(raw, EXECUTION_KEYS, label=f"executor for {task_id}")
        self._spend(result.get("usage"))
        if _text(result.get("error")):
            self._publish("council_error", turn_id=turn_id, task_id=task_id, actor_id=owner,
                          error=_text(result.get("error")), phase="execute")
        status = _text(result.get("status"))
        if not status:
            return
        try:
            self._ledger.set_task_status(task_id, status, actor=owner,
                                         note=_text(result.get("run_id")))
        except CouncilError as exc:
            # The ledger refused: an open blocking objection, or a status that
            # is not in the vocabulary.  That refusal IS the rule working, so it
            # is announced rather than swallowed (12.1).
            logger.info("council orchestrator %s: the ledger refused %s for %s: %s",
                        self.session_id, status, task_id, exc)
            self._publish("council_activity_blocked", turn_id=turn_id, task_id=task_id,
                          actor_id=owner, reason=str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - never fatal on the turn path
            logger.warning("council orchestrator %s: task %s status could not be recorded: %s",
                           self.session_id, task_id, exc)
            return
        # Deliberately NOT `council_activity_verified`: a task the executor
        # reported as done is work that finished, not work that was verified.
        # `prove` issues that verdict and `synthesis.status_of` reads it; a room
        # that published "verified" here would be presenting an executor's word
        # as evidence, which is the failure 18-Phase-4 exists to prevent.
        self._publish("council_turn_state", turn_id=turn_id, task_id=task_id, actor_id=owner,
                      task_status=status, run_id=_text(result.get("run_id")),
                      state="delegating")
        # ...and this is where the evidence is actually asked for.  It runs
        # AFTER the status is recorded, so a verification that fails leaves a
        # `done` task rather than losing the run, and a verification that
        # succeeds is the only way `verified` is ever written.
        if status in VERIFIABLE_TASK_STATUSES:
            await self._verify(turn_id, task_id, owner, result)

    async def _verify(self, turn_id: str, task_id: str, owner: str,
                      result: Mapping[str, Any]) -> None:
        """Reconcile what the run CLAIMED with what Faustus OBSERVED (12.1).

        The whole of the plan's "ninguna tarea con escritura se presenta como
        verificada solo por afirmación del agente" lives on this path, and every
        branch in it can only ever LOWER the claim:

        * no verifier in this build, or it raised -> the task keeps `done`;
        * `prove` did not say `proved` -> the task keeps `done` and the packet is
          still kept, because "we looked and could not show it" is a result the
          close has to be able to state;
        * the ledger refuses `verified` over an open blocking objection -> the
          task keeps `done` and the refusal is announced.

        Only a `proved` packet AND a ledger that consents produce `verified` and
        a `council_activity_verified` event.  `verify_task` is synchronous and
        reads the workspace, so it runs in a worker thread: a room whose event
        loop is blocked on a checkpoint diff cannot answer a `pause`.
        """
        verifier = self._verify_fn()
        if verifier is None:
            return
        task = self._task(task_id)
        if task is None:
            logger.debug("council orchestrator %s: %s vanished before verification",
                         self.session_id, task_id)
            return
        try:
            report = await asyncio.to_thread(
                verifier, task=task, session=self.session,
                changes=dict(result.get("changes") or {}),
                verification=dict(result.get("verification") or {}))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - unproved is the honest answer
            logger.warning("council orchestrator %s: verifying %s failed: %s",
                           self.session_id, task_id, exc)
            self._publish("council_error", turn_id=turn_id, task_id=task_id, actor_id=owner,
                          error=f"{type(exc).__name__}: {exc}", phase="verify")
            return
        report = dict(report or {})
        proof = dict(report.get("proof") or {})
        # The packet is kept whatever the verdict: `synthesis._verification`
        # reports `contradicted` and `unproved` too, and a close that only ever
        # carried good news would be worth less than no close at all.
        if proof:
            self._proofs[task_id] = proof
        verdict = _text(report.get("verdict"))
        if verdict != VERIFIED_VERDICT:
            logger.info("council orchestrator %s: %s came back %r; it keeps the status the "
                        "run earned it", self.session_id, task_id, verdict or "unproved")
            self._publish("council_turn_state", turn_id=turn_id, task_id=task_id,
                          actor_id=owner, state="verifying", verdict=verdict or "unproved",
                          changeset_id=_text(report.get("changeset_id")))
            return
        allowed, why = True, ""
        can_verify = getattr(self._ledger, "can_verify", None)
        if callable(can_verify):
            try:
                allowed, why = can_verify(task_id)
            except Exception as exc:  # noqa: BLE001 - a veto that cannot be read is a veto
                allowed, why = False, f"the objection check failed: {exc}"
        if not allowed:
            logger.info("council orchestrator %s: %s is proved and still not verified: %s",
                        self.session_id, task_id, why)
            self._publish("council_activity_blocked", turn_id=turn_id, task_id=task_id,
                          actor_id=owner, reason=why or "an open objection blocks verification")
            return
        try:
            self._ledger.set_task_status(task_id, VERIFIED_STATUS, actor=owner,
                                         note=_text(report.get("changeset_id")))
        except Exception as exc:  # noqa: BLE001 - the proof stands, the status does not
            logger.warning("council orchestrator %s: %s is proved but could not be recorded "
                           "as verified: %s", self.session_id, task_id, exc)
            return
        self._publish("council_activity_verified", turn_id=turn_id, activity_id=turn_id,
                      task_id=task_id, actor_id=owner, verdict=verdict,
                      changeset_id=_text(report.get("changeset_id")),
                      proof_id=_text(proof.get("identity") or proof.get("proof_id")),
                      confidence=report.get("confidence"))

    def _verify_fn(self) -> Optional[Callable[..., Any]]:
        """`adapters.verify_task`, the injected double, or `None`.

        `None` is not a failure state to work around: a build without
        `changesets` or `prove` simply cannot verify anything, and a room there
        reports `done` and stops, which is the truthful answer."""
        if self._verifier is not None:
            return self._verifier
        try:
            from src.council.adapters import verify_task

            self._verifier = verify_task
            return verify_task
        except Exception as exc:  # noqa: BLE001 - a missing engine is a fact
            logger.warning("council orchestrator %s: no task verifier in this build (%s); "
                           "tasks will close as `done` and never as `verified`",
                           self.session_id, exc)
            return None

    def _proof_packet(self) -> Optional[Dict[str, Any]]:
        """The one proof packet the close is built against, or `None`.

        A council can run several tasks and `synthesis.status_of` takes ONE
        packet, so the room hands it the weakest verdict it collected: a session
        where four tasks were proved and one was contradicted is a session whose
        close must not read `verified`.  With no packets at all the answer is
        `None`, which is what makes `verified` unreachable rather than assumed.
        """
        if not self._proofs:
            return None
        packets = list(self._proofs.values())
        for verdict in WEAKEST_PROOF_VERDICTS:
            for packet in packets:
                if _text(packet.get("verdict")).lower() == verdict:
                    return packet
        return packets[0]


    # -- commands (13) -----------------------------------------------------

    def _set_session_status(self, status: str) -> Dict[str, Any]:
        """Move the room's status under optimistic revision (8).

        Two contradictory orders written against the same revision cannot both
        land: the second is told the revision that actually won and gets its
        change refused, rather than silently overwriting a `pause` nobody sees
        again.  A refusal is a value and not an exception, because these are
        commands a route maps to a response."""
        try:
            updated = self._store.update_session(
                self.session_id, {"status": status}, expected_revision=self._revision)
        except Exception as exc:  # noqa: BLE001 - conflicts are answers
            actual = getattr(exc, "revision", None)
            if actual is not None:
                logger.info("council orchestrator %s: %r lost to revision %s",
                            self.session_id, status, actual)
                return {"ok": False, "conflict": True, "status": status,
                        "expected_revision": self._revision, "revision": int(actual),
                        "reason": str(exc)}
            logger.warning("council orchestrator %s: status %r refused: %s",
                           self.session_id, status, exc)
            return {"ok": False, "conflict": False, "status": status, "reason": str(exc)}
        self._revision = int(_get(updated, "revision", self._revision) or self._revision)
        self.session = updated
        return {"ok": True, "conflict": False, "status": _text(_get(updated, "status", status)),
                "revision": self._revision}

    async def pause(self) -> Dict[str, Any]:
        """Start nothing further; finish what is already running.

        A tool that cannot be cancelled is not killed (8).  Pausing sets the
        flag the round checks before each participant and before each task, so
        an in-flight generation or a running command completes and is recorded
        -- half-killing a write is how a repository ends up in a state nobody
        intended, and the user asked for a pause, not for a rollback."""
        async with self._guard():
            self._paused = True
            outcome = self._set_session_status("paused")
            self._publish("council_activity_blocked", reason="paused by the user",
                          persisted=outcome["ok"])
            return {**outcome, "paused": True,
                    "note": "no new call will start; work already in flight finishes and is "
                            "recorded"}

    async def resume(self) -> Dict[str, Any]:
        """Let the room start calls again."""
        async with self._guard():
            outcome = self._set_session_status("active")
            if outcome["ok"] or not outcome.get("conflict"):
                self._paused = False
            self._publish("council_turn_state", state="running", resumed=True,
                          persisted=outcome["ok"])
            return {**outcome, "paused": self._paused}

    async def cancel_turn(self, turn_id: str, *, actor: str) -> Dict[str, Any]:
        """Stop a turn, release what is safe to release, record what is not.

        Three things happen and the third is the one that matters: the turn is
        marked cancelled (so a late answer cannot advance it), the participants'
        claims are released through `ledger.release_on_cancel`, and every claim
        whose resource a mutating tool is still writing is KEPT and reported.
        Releasing one of those would let a second writer in while the first is
        still finishing (20)."""
        target = _text(turn_id)
        async with self._guard():
            self._cancelled.add(target)
            turn = None
            try:
                turn = self._store.get_turn(target)
            except Exception as exc:  # noqa: BLE001 - read path
                logger.warning("council orchestrator %s: turn %s unreadable while cancelling: %s",
                               self.session_id, target, exc)
            state = _text(_get(turn, "state", "")) if turn is not None else ""
            if turn is not None and not is_terminal_turn(state):
                self._advance(turn, "cancelled", stop_reason="user_stopped", actor_id=_text(actor))

            released: List[Dict[str, Any]] = []
            kept: List[Dict[str, Any]] = []
            for entry in self._participants():
                inner = getattr(entry, "participant", entry)
                participant_id = entry if isinstance(entry, str) else _text(_get(inner, "id", ""))
                if not participant_id:
                    continue
                report = self._release(participant_id)
                released.extend(report.get("released", ()))
                kept.extend(report.get("kept", ()))
            for row in released:
                # Built explicitly rather than splatted: a released row that
                # grew a `reason` key upstream would collide with this one and
                # turn a cancel into a TypeError.
                self._publish("council_claim_released", turn_id=target, reason="cancelled",
                              actor_id=_text(actor), claim_id=row.get("claim_id", ""),
                              kind=row.get("kind", ""), resource=row.get("resource", ""))
            if kept:
                logger.info("council orchestrator %s: cancel kept %d resource(s) still in use",
                            self.session_id, len(kept))
                self._publish("council_activity_blocked", turn_id=target,
                              reason="a mutating tool is still running on "
                                     f"{len(kept)} claimed resource(s); they were not released",
                              kept=[row.get("resource", "") for row in kept])
            return {"ok": True, "turn_id": target, "state": "cancelled", "actor": _text(actor),
                    "released": released, "kept": kept,
                    "late_results": len(self._late)}

    def _release(self, participant_id: str) -> Dict[str, Any]:
        """`ledger.release_on_cancel` for one seat, with what it is still writing."""
        try:
            return dict(self._ledger.release_on_cancel(
                participant_id,
                mutating_active_resources=self._mutating.get(participant_id, ())) or {})
        except Exception as exc:  # noqa: BLE001 - a cancel never raises
            logger.warning("council orchestrator %s: releasing %s's claims failed: %s",
                           self.session_id, participant_id, exc)
            return {"released": [], "kept": []}

    async def stop_participant(self, participant_id: str, *, actor: str) -> Dict[str, Any]:
        """Take one participant out of the room's rounds.

        It is not asked again until somebody resumes it, and what it holds is
        released on the same "only when it is safe" rule as a cancel."""
        seat = _text(participant_id)
        async with self._guard():
            self._stopped.add(seat)
            report = self._release(seat)
            self._publish("council_activity_blocked", actor_id=seat, reason="stopped by the user",
                          stopped_by=_text(actor))
            for row in report.get("released", ()):
                self._publish("council_claim_released", reason="participant stopped",
                              actor_id=seat, claim_id=row.get("claim_id", ""),
                              kind=row.get("kind", ""), resource=row.get("resource", ""))
            return {"ok": True, "participant_id": seat, "actor": _text(actor),
                    "released": list(report.get("released", ())),
                    "kept": list(report.get("kept", ()))}

    async def steer(self, participant_id: str, message: str, *, actor: str) -> Dict[str, Any]:
        """Send one participant a private instruction from the user.

        It is a message, addressed and visible to that participant, from the
        actor who sent it -- not a system prompt, not a permission and not a
        tool grant (3.1: the coordinator organises and does not widen anything).
        The next packet built for that seat carries it as what it is."""
        seat = _text(participant_id)
        async with self._guard():
            message_id = self._commit_message(
                author_id=_text(actor), author_kind="user", audience=[seat],
                message_type="message", content=str(message or ""), visibility="participant",
                metadata={"steer": True, "target": seat})
            return {"ok": bool(message_id), "participant_id": seat, "message_id": message_id,
                    "actor": _text(actor),
                    "note": "a steer changes what a participant is told, never what it may do"}

    async def handoff_task(self, task_id: str, to: str, *, actor: str) -> Dict[str, Any]:
        """Move a task and its claims to another participant (11.3).

        The order is the protocol: the claims move first, one at a time and
        through the ledger, and the ownership changes only if every one of them
        moved.  A handoff refused because the current owner still has a mutating
        tool running leaves everything exactly where it was -- handing a
        half-written file to a second writer is the failure 11.3 is written
        against, and no later apology reconstructs the file."""
        target = _text(to)
        ident = _text(task_id)
        async with self._guard():
            task = self._task(ident)
            if task is None:
                return {"ok": False, "task_id": ident, "to": target,
                        "reason": f"no task {ident!r} in this room"}
            if not target:
                return {"ok": False, "task_id": ident, "to": target,
                        "reason": "a handoff needs a new owner"}
            owner = _text(_get(task, "owner_participant_id", ""))
            mutating = bool(self._mutating.get(owner)) or owner in self._running_tasks
            if mutating:
                # Step 3 of 11.3, and it is checked here rather than only per
                # claim: a task whose executor is running but which has not
                # claimed anything yet would otherwise change owner underneath
                # a live run, and the new owner would inherit a result it never
                # asked for.
                reason = (f"a mutating tool of {owner} is still running for {ident}; finish or "
                          f"pause it before handing the task to {target}")
                self._publish("council_activity_blocked", task_id=ident, actor_id=owner,
                              reason=reason)
                return {"ok": False, "task_id": ident, "from": owner, "to": target,
                        "reason": reason, "claims": []}
            moved: List[Dict[str, Any]] = []
            for claim in self._claims_of(owner, ident):
                report = dict(self._ledger.handoff(_text(_get(claim, "id", "")), to=target,
                                                   actor=_text(actor),
                                                   mutating_active=mutating) or {})
                moved.append(report)
                if not report.get("ok"):
                    self._publish("council_activity_blocked", task_id=ident, actor_id=owner,
                                  reason=_text(report.get("reason")))
                    return {"ok": False, "task_id": ident, "from": owner, "to": target,
                            "reason": _text(report.get("reason")), "claims": moved}
            try:
                self._ledger.assign(ident, target, actor=_text(actor))
            except Exception as exc:  # noqa: BLE001 - a refused command is an answer
                logger.warning("council orchestrator %s: task %s could not be reassigned: %s",
                               self.session_id, ident, exc)
                return {"ok": False, "task_id": ident, "from": owner, "to": target,
                        "reason": str(exc), "claims": moved}
            self._publish("council_task_handed_off", task_id=ident, actor_id=_text(actor),
                          previous_owner=owner, new_owner=target,
                          claims=[_text(_get(c, "id", "")) for c in moved])
            return {"ok": True, "task_id": ident, "from": owner, "to": target,
                    "claims": moved}

    def _task(self, task_id: str) -> Any:
        try:
            for task in self._ledger.tasks() or ():
                if _text(_get(task, "id", "")) == task_id:
                    return task
        except Exception as exc:  # noqa: BLE001 - read path
            logger.warning("council orchestrator %s: tasks unreadable: %s", self.session_id, exc)
        return None

    def _claims_of(self, holder_id: str, task_id: str) -> List[Any]:
        """The claims this holder took FOR THIS TASK, and no others.

        Deliberately not "everything this participant holds": a driver may hold
        a claim for a different task, or for work the room never gave it a task
        for, and moving those along with the task would be a land grab wearing a
        handoff's clothes (11.3)."""
        try:
            return [claim for claim in (self._ledger.claims_of(holder_id) or ())
                    if _text(_get(claim, "task_id", "")) == task_id]
        except Exception as exc:  # noqa: BLE001 - read path
            logger.warning("council orchestrator %s: claims of %s unreadable: %s",
                           self.session_id, holder_id, exc)
            return []

    async def request_synthesis(self) -> TurnOutcome:
        """Close the activity now, from the ledger.

        The user asked, so the stop reason is `user_stopped` unless a budget
        already ran out and named a better one.  The summary is
        `synthesis.build()`'s -- decisions, changes, verification, open
        objections, contributions and consumption, all read off the ledger and
        none of it re-narrated from the transcript (12.2)."""
        async with self._guard():
            stop_reason = self._scheduler.stop_reason() or "user_stopped"
            summary = self._summary(stop_reason)
            status = _text(_get(summary, "status", "")) if summary else ""
            self._publish("council_activity_completed", stop_reason=stop_reason,
                          status=status, requested=True)
            return TurnOutcome(turn_id="", state="synthesizing", stop_reason=stop_reason,
                               messages=(), summary=summary,
                               blocked_on="" if summary else "the summary could not be built")

    def state(self) -> Dict[str, Any]:
        """Everything a route, a page or a test needs to know about this room.

        Read-only and total: it never raises, because the one moment somebody
        asks a room what it is doing is the moment it is misbehaving."""
        snapshot: Dict[str, Any] = {}
        try:
            snapshot = dict(self._ledger.snapshot() or {})
        except Exception as exc:  # noqa: BLE001 - read path
            logger.debug("council orchestrator %s: ledger snapshot unreadable: %s",
                         self.session_id, exc)
        stats: Dict[str, Any] = {}
        try:
            stats = dict(self._scheduler.stats() or {})
        except Exception as exc:  # noqa: BLE001 - read path
            logger.debug("council orchestrator %s: scheduler stats unreadable: %s",
                         self.session_id, exc)
        return {
            "session_id": self.session_id,
            "policy": self._policy_name(),
            "revision": self._revision,
            "paused": self._paused,
            "cancelled_turns": sorted(self._cancelled),
            "stopped_participants": sorted(self._stopped),
            "mutating": {seat: list(res) for seat, res in self._mutating.items()},
            "running_tasks": dict(self._running_tasks),
            "late_results": list(self._late),
            "counts": dict(snapshot.get("counts") or {}),
            "blocking_open": bool(snapshot.get("blocking_open")),
            "usage": self._usage(),
            "scheduler": stats,
            "invoker": type(self._invoker).__name__ if self._invoker is not None else "",
            "executor": type(self._executor).__name__ if self._executor is not None else "",
            "elapsed_s": round(max(0.0, float(self._clock()) - self._started), 3),
        }


# -- the defaults, resolved lazily -----------------------------------------
#
# Each one is the module that already owns that concern (6.3).  They are
# functions rather than constructor defaults so that a room can be built in a
# test without opening a database, and so that a broken subsystem degrades to a
# named `None` instead of stopping the import of everything downstream.

def _default_store() -> Any:
    from src.council.persistence import store

    return store()


def _default_ledger(session_id: str, workspace: str) -> Any:
    from src.council.ledger import CouncilLedger

    return CouncilLedger(session_id or "council_unknown", workspace=workspace)


def _default_scheduler(session: Any) -> Any:
    from src.council.scheduler import scheduler_for

    return scheduler_for(session)


def _default_events(session_id: str) -> Any:
    from src.council.events import stream_for

    return stream_for(session_id or "council_unknown")
