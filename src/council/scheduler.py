"""
council/scheduler.py — who may call a model right now, and who has to wait.

The failure this file exists to prevent is measured and written down twice in
this tree already.  `src/agent_tools/subagent_tools.py` states one half: a
chat's `/agents` and two dispatched jobs running at the same time used to put
3 x N workers against one Ollama, every one of them queueing on the model's
single slot while its own wall-clock timeout ticked down — so the work did not
go faster, it just failed later.  `src/tournament.py` states the other half,
measured on this machine (FAUSTUS.md section 20): two requests to the SAME
model serialize behind one llama-server slot, while two DIFFERENT models
genuinely generate at the same time.

A council makes both worse, because a room is several models by definition and
a policy that "asks everyone" is one line of code.  So this module is the one
place where a participant asks for its turn on the machine, and the answer may
be "wait", "no", or "not within your timeout".

What it does NOT do is own any of those primitives (plan section 25: do not
duplicate the GPU scheduler).  The per-model lock is `tournament.model_lock`
and the GPU semaphore is `tournament.gpu_slots`, which is
`subagent_tools.shared_slots` — the very objects a tournament and a delegation
are already waiting on.  Sharing them is the whole point: a council with its
own private semaphore is two subsystems each believing it owns the one GPU.

ACQUISITION ORDER — always this, never any other:

    1. this room's own parallel limiter (lives here),
    2. the per-model lock (`tournament.model_lock`),
    3. the machine-wide GPU slot (`tournament.gpu_slots`),

and released in reverse.  Step 2 before step 3 is not a preference: it is the
order `tournament._Gate` already uses, and we wait on the SAME two objects it
waits on.  Two subsystems taking one pair of primitives in two orders is the
textbook deadlock, and this one would be unreproducible — it needs a tournament
and a council alive in the same minute.  Concretely, the order makes the cycle
impossible: nothing that holds a GPU slot ever waits for a model lock, and
nothing that holds a model lock ever waits for a parallel slot.

The budget is the other half.  Section 3.6 makes cost and latency part of the
contract, and section 20 asks for one precise behaviour: exhausting the budget
prevents STARTING another call.  It does not kill the call already in flight.
A room that cut a generation short to save tokens would pay for those tokens
anyway with nothing to show for them, and — much worse — could leave an effect
half applied.  So `can_start()` is asked before a reservation, `reserve()`
refuses when the answer is no, and `release()` never consults the budget at
all.  `stop_reason()` names WHICH limit ran out — `max_rounds`, `max_turns` or
`budget_exhausted` — because "it finished" and "it ran out of money" must never
be the same sentence (section 12.3).

THREADING.  The counters are guarded by a `threading.RLock` and are safe to
read from any thread; the lock is held for arithmetic only and never across an
`await`, so nothing here blocks the event loop.  `reserve()` and `release()`
are coroutines and live in an event loop: the things they wait on are asyncio
primitives, and — exactly like `tournament.model_lock` and
`subagent_tools.shared_slots` — they are keyed by the running loop, because an
asyncio primitive binds to the loop that first waits on it and the test suite
runs one loop per test.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, MutableMapping, Optional, Tuple

from .contracts import STOP_REASONS, CouncilBudgets, new_id

logger = logging.getLogger(__name__)

__all__ = [
    "ACQUISITION_ORDER",
    "LIMIT_NAMES",
    "NO_GPU_SLOTS",
    "BudgetState",
    "Reservation",
    "CouncilScheduler",
    "scheduler_for",
    "reset_schedulers",
]


#: The order every reservation is taken in, as data rather than as a habit, so
#: a test can assert it and a reader does not have to trust the docstring.
ACQUISITION_ORDER: Tuple[str, ...] = ("parallel", "model_lock", "gpu_slot")

#: The limits `BudgetState.exhausted` may name, in the order they are checked.
#: The order is fixed on purpose: when two limits are out at once the answer
#: must be the same on every call, or a room reports a different reason to the
#: log than it reports to the user.
LIMIT_NAMES: Tuple[str, ...] = (
    "max_rounds", "max_turns", "max_wall_seconds", "max_total_tokens",
    "max_parallel",
)

#: Which stop reason each limit produces (sections 12.3 and 20).  Rounds and
#: turns keep their own names because a room that used up its rounds and a room
#: that used up its money stopped for two different reasons and the user is
#: owed the difference; wall clock, tokens and a zeroed parallelism are all
#: "the budget is spent".  Every value here is in `STOP_REASONS`, and
#: `tests/test_council_scheduler.py` asserts it rather than trusting this line.
_STOP_FOR: Dict[str, str] = {
    "max_rounds": "max_rounds",
    "max_turns": "max_turns",
    "max_wall_seconds": "budget_exhausted",
    "max_total_tokens": "budget_exhausted",
    "max_parallel": "budget_exhausted",
}

#: A reservation that waits longer than this by default has stopped being a
#: queue and started being a hang.  Callers override it per call.
DEFAULT_RESERVE_TIMEOUT_S = 60.0

_MAX_CACHED_LOCKS = 256


class _NoSlots:
    """A GPU semaphore that is deliberately absent.

    `gpu_slots=None` means "ask `tournament` for the shared one"; passing
    `NO_GPU_SLOTS` means "this room does not wait on the GPU at all".  Those
    are two different intentions and they get two different values, instead of
    one `None` a reader has to guess at.  Unit tests use it so that importing
    a scheduler never drags the machine-wide semaphore into a test process.
    """

    __slots__ = ()

    async def acquire(self) -> bool:
        return True

    def release(self) -> None:
        return None

    def __repr__(self) -> str:  # pragma: no cover - debugging affordance
        return "NO_GPU_SLOTS"


NO_GPU_SLOTS = _NoSlots()


# ── the primitives we borrow, and the ones we only fall back to ────────────

_FALLBACK_LOCKS: Dict[Tuple[int, str], asyncio.Lock] = {}


def _loop_key() -> int:
    """The identity of the running loop, or 0 outside one.

    Same keying as `tournament.model_lock` and `subagent_tools.shared_slots`,
    for the same reason: an asyncio primitive binds to the loop that first
    waits on it, and a primitive cached across two loops raises instead of
    synchronising.
    """
    try:
        return id(asyncio.get_running_loop())
    except RuntimeError:
        return 0


def _fallback_model_lock(model: str) -> asyncio.Lock:
    """A per-model lock for the case where `src.tournament` cannot be imported.

    This is NOT a second implementation of the model lock competing with the
    first: it is only ever reached when the real one is unavailable, and when
    that happens serialising within the council still beats serialising
    nowhere.  If both a tournament and a council reach this path in the same
    process, they are already not sharing a lock — but nothing in this tree
    can import `tournament` and fail, so in practice the branch is a seat belt.
    """
    key = (_loop_key(), str(model or ""))
    lock = _FALLBACK_LOCKS.get(key)
    if lock is None:
        if len(_FALLBACK_LOCKS) > _MAX_CACHED_LOCKS:
            for stale, held in [(k, v) for k, v in _FALLBACK_LOCKS.items()
                                if not v.locked()]:
                _FALLBACK_LOCKS.pop(stale, None)
        lock = _FALLBACK_LOCKS[key] = asyncio.Lock()
    return lock


def _tournament_model_lock(model: str) -> asyncio.Lock:
    """`tournament.model_lock`, imported lazily so that importing a scheduler
    never drags a model client onto the turn path."""
    try:
        from src.tournament import model_lock
        return model_lock(model)
    except Exception as e:  # noqa: BLE001 - a room schedules without it
        logger.debug("council scheduler: tournament.model_lock unavailable: %s", e)
        return _fallback_model_lock(model)


def _tournament_gpu_slots() -> Any:
    """`tournament.gpu_slots`, which is `subagent_tools.shared_slots`.

    Returns `None` when the machine has no semaphore configured, which is what
    `tournament` itself does: a room without a GPU gate degrades to the model
    locks alone rather than refusing to run.
    """
    try:
        from src.tournament import gpu_slots
        return gpu_slots("")
    except Exception as e:  # noqa: BLE001 - a room schedules without it
        logger.debug("council scheduler: shared GPU slots unavailable: %s", e)
        return None


# ── what a caller holds and what a room has spent ──────────────────────────

@dataclass(frozen=True)
class Reservation:
    """One participant's turn on the machine.

    `token` is the handle `release()` takes, and it is a token rather than the
    object graph on purpose: a caller cannot release half of a reservation, and
    a reservation that was already released is a no-op instead of an exception
    on the turn path.  `waited_ms` is kept on the reservation because the queue
    time is the number that explains a slow room, and section 21 asks for it
    per participant.
    """

    participant_id: str
    model: str
    token: str
    acquired_at: float
    waited_ms: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "participant_id": self.participant_id,
            "model": self.model,
            "token": self.token,
            "acquired_at": self.acquired_at,
            "waited_ms": self.waited_ms,
        }


@dataclass(frozen=True)
class BudgetState:
    """What the room has SPENT, and the name of the limit that ran out.

    Every field is a spend, not a remainder: a remainder is only meaningful
    next to the limit it came from, and two numbers that must be read together
    end up read apart.  `exhausted` is the name of the limit
    (`"max_rounds"`, `"max_total_tokens"`, ...) or `""`; `parallel` is how many
    reservations are in flight right now, which is a level and not a spend, and
    is here because every other view of "what is this room doing" needs it.
    """

    rounds: int
    turns: int
    wall_seconds: float
    total_tokens: int
    parallel: int
    exhausted: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rounds": self.rounds,
            "turns": self.turns,
            "wall_seconds": self.wall_seconds,
            "total_tokens": self.total_tokens,
            "parallel": self.parallel,
            "exhausted": self.exhausted,
        }


@dataclass
class _Held:
    """The three primitives one reservation is holding, so `release()` can give
    them back in the reverse of `ACQUISITION_ORDER` without guessing."""

    participant_id: str
    model: str
    parallel: Optional[asyncio.Semaphore]
    lock: Optional[asyncio.Lock]
    slots: Any
    acquired_at: float


def _as_budgets(raw: Any) -> CouncilBudgets:
    """Accept the contract, a mapping or nothing.

    A malformed mapping raises here, at construction time, rather than being
    silently replaced by the defaults: a typo in a budget that quietly becomes
    "the generous default" is a bill nobody agreed to.  Construction is not the
    hot path; `reserve()` and `spend()` are, and neither of them raises.
    """
    if raw is None:
        return CouncilBudgets()
    if isinstance(raw, CouncilBudgets):
        return raw
    return CouncilBudgets.parse(raw, "budgets")


# ── the scheduler ──────────────────────────────────────────────────────────

class CouncilScheduler:
    """The room's turn on the machine, and the room's budget.

    One per session.  Two calls to the same model serialize; calls to distinct
    models overlap up to the room's parallel limit and the machine's GPU slots
    (section 3.6).  A budget that runs out prevents the NEXT reservation and
    never interrupts one already granted (section 20).

    Lives in one event loop.  `reserve()` and `release()` are coroutines;
    `spend()`, `state()`, `can_start()`, `stop_reason()`, `waited_ms()` and
    `stats()` are plain methods, guarded by a `threading.RLock`, and safe to
    call from a reporting thread while a turn is running.
    """

    def __init__(self, session_id: str, *, budgets: Any,
                 max_parallel: int = 2, gpu_slots: Any = None,
                 model_locks: Any = None,
                 clock: Optional[Callable[[], float]] = None) -> None:
        self.session_id = str(session_id or "")
        self.budgets = _as_budgets(budgets)
        # A constructor argument may NARROW the room's parallelism and never
        # widen it — the same rule section 11.1 gives for tool profiles, applied
        # to concurrency: the budget is what the user authorised.  This is why
        # `scheduler_for()` passes the session's own `max_parallel` explicitly
        # instead of leaving the default 2 to silently cap a wider budget.
        self.max_parallel = max(0, min(int(max_parallel),
                                       int(self.budgets.max_parallel)))
        self._gpu_arg = gpu_slots
        self._gpu: Any = None
        self._gpu_resolved = False
        self._locks = model_locks
        self._clock: Callable[[], float] = clock or time.monotonic
        self._started = float(self._clock())
        self._guard = threading.RLock()
        # Keyed by loop for the same reason every other primitive in this file
        # is: one loop per test, and a semaphore bound to a dead loop is worse
        # than no semaphore.
        self._parallel: Dict[int, asyncio.Semaphore] = {}
        self._held: Dict[str, _Held] = {}
        self._waited: Dict[str, int] = {}
        self._rounds = 0
        self._turns = 0
        self._tokens = 0
        self._in_flight = 0
        self._peak = 0
        self._granted = 0
        self._released = 0
        self._timeouts = 0
        self._refused = 0

    # ── the primitives, resolved late ──────────────────────────────────────

    def _parallel_semaphore(self) -> asyncio.Semaphore:
        key = _loop_key()
        sem = self._parallel.get(key)
        if sem is None:
            # `max(1, ...)`: a limit of 0 is refused by `can_start()` long
            # before anything reaches here, and a Semaphore(0) would hang
            # rather than say no.
            sem = self._parallel[key] = asyncio.Semaphore(max(1, self.max_parallel))
        return sem

    def _slots(self) -> Any:
        """The GPU semaphore this room waits on, resolved on first use.

        Late, because `subagent_tools.shared_slots` keys by the running loop
        and a scheduler is often built before there is one.
        """
        if self._gpu_resolved:
            return self._gpu
        arg = self._gpu_arg
        if arg is None:
            self._gpu = _tournament_gpu_slots()
        elif arg is NO_GPU_SLOTS:
            self._gpu = None
        else:
            self._gpu = arg
        self._gpu_resolved = True
        logger.debug("council scheduler %s: GPU slots = %r", self.session_id, self._gpu)
        return self._gpu

    def _model_lock(self, model: str) -> asyncio.Lock:
        """The lock that makes two calls to one model take turns.

        `model_locks` may be a callable (`tournament.model_lock` is one) or a
        mutable mapping a test owns.  `None` means the shared one, which is the
        answer in production: a council and a tournament asking for the same
        model must queue on the SAME lock or the machine is oversubscribed by
        exactly the number of subsystems that forgot.
        """
        source = self._locks
        if source is None:
            return _tournament_model_lock(model)
        if callable(source):
            return source(model)
        if isinstance(source, MutableMapping):
            lock = source.get(model)
            if lock is None:
                lock = source[model] = asyncio.Lock()
            return lock
        return _tournament_model_lock(model)


    # ── reserving and releasing ────────────────────────────────────────────

    async def _acquire(self, participant_id: str, model: str) -> _Held:
        """Take the three primitives in `ACQUISITION_ORDER`, or take none.

        The `except BaseException` is load-bearing and covers the cancellation
        `asyncio.wait_for` sends on a timeout: a reservation abandoned halfway
        that kept a model lock would wedge that model for the life of the
        process, which is the failure this whole module is about.
        """
        parallel = self._parallel_semaphore()
        lock = self._model_lock(model)
        slots = self._slots()
        got_parallel = False
        got_lock = False
        try:
            await parallel.acquire()
            got_parallel = True
            await lock.acquire()
            got_lock = True
            if slots is not None:
                await slots.acquire()
            return _Held(participant_id=participant_id, model=model,
                         parallel=parallel, lock=lock, slots=slots,
                         acquired_at=float(self._clock()))
        except BaseException:
            if got_lock:
                self._give_back_lock(lock)
            if got_parallel:
                self._give_back_sem(parallel)
            raise

    async def reserve(self, participant_id: str, model: str, *,
                      timeout_s: float = DEFAULT_RESERVE_TIMEOUT_S) -> Optional[Reservation]:
        """Wait for this participant's turn on `model`, or answer `None`.

        `None` has two causes and both are logged with which one it was: the
        budget says no more calls may START (section 20), or the wait ran past
        `timeout_s`.  It is never an exception, because a participant that
        cannot get a slot is a normal outcome of a busy machine and a room must
        be able to record it and move on.

        A `timeout_s` of `None` waits forever; anything <= 0 takes what is free
        this instant and gives up otherwise.
        """
        pid = str(participant_id or "")
        mdl = str(model or "")
        allowed, why = self.can_start()
        if not allowed:
            with self._guard:
                self._refused += 1
            logger.info("council scheduler %s: refused %s on %r — %s",
                        self.session_id, pid or "?", mdl, why)
            return None

        started = float(self._clock())
        timeout = None if timeout_s is None else max(0.0, float(timeout_s))
        try:
            held = await asyncio.wait_for(self._acquire(pid, mdl), timeout)
        except asyncio.TimeoutError:
            waited = self._ms_since(started)
            with self._guard:
                self._timeouts += 1
                self._waited[pid] = self._waited.get(pid, 0) + waited
            logger.warning(
                "council scheduler %s: %s gave up after %d ms waiting for %r "
                "(timeout %ss); the model is busy, nothing was reserved",
                self.session_id, pid or "?", waited, mdl, timeout)
            return None
        except Exception as e:  # noqa: BLE001 - a reservation never raises at a turn
            logger.warning("council scheduler %s: reserving %r for %s failed: %s",
                           self.session_id, mdl, pid or "?", e)
            return None

        waited = self._ms_since(started)
        token = new_id("res")
        with self._guard:
            self._held[token] = held
            self._waited[pid] = self._waited.get(pid, 0) + waited
            self._granted += 1
            self._in_flight += 1
            self._peak = max(self._peak, self._in_flight)
        logger.debug("council scheduler %s: %s holds %r after %d ms (%d in flight)",
                     self.session_id, pid or "?", mdl, waited, self._in_flight)
        return Reservation(participant_id=pid, model=mdl, token=token,
                           acquired_at=held.acquired_at, waited_ms=waited)

    async def release(self, reservation: Reservation) -> None:
        """Give the three primitives back, in the reverse of the order they
        were taken.  Idempotent: releasing twice, or releasing something this
        scheduler never granted, logs and returns.  It does not consult the
        budget — an exhausted budget stops the next call, never this one."""
        token = str(getattr(reservation, "token", "") or "")
        with self._guard:
            held = self._held.pop(token, None)
            if held is None:
                logger.debug("council scheduler %s: release of an unknown or "
                             "already-released reservation %r", self.session_id, token)
                return
            self._in_flight = max(0, self._in_flight - 1)
            self._released += 1
        if held.slots is not None:
            self._give_back_sem(held.slots)
        if held.lock is not None:
            self._give_back_lock(held.lock)
        if held.parallel is not None:
            self._give_back_sem(held.parallel)

    @staticmethod
    def _give_back_lock(lock: asyncio.Lock) -> None:
        try:
            lock.release()
        except RuntimeError:
            pass
        except Exception as e:  # noqa: BLE001 - a release never masks the caller's error
            logger.debug("council scheduler: releasing a model lock failed: %s", e)

    @staticmethod
    def _give_back_sem(sem: Any) -> None:
        try:
            sem.release()
        except Exception as e:  # noqa: BLE001 - same reason
            logger.debug("council scheduler: releasing a semaphore failed: %s", e)


    # ── the budget ─────────────────────────────────────────────────────────

    def _ms_since(self, started: float) -> int:
        return max(0, int(round((float(self._clock()) - float(started)) * 1000)))

    def spend(self, *, tokens: int = 0, turns: int = 0, rounds: int = 0) -> BudgetState:
        """Record what a turn cost and answer with the room's new state.

        Never raises and never refuses: spending is a report of what already
        happened, and a scheduler that rejected the report would only make the
        room's own accounting the least accurate thing about it.  Negative
        amounts are clamped to zero and logged — a refund is not a thing a
        budget can have here, because the tokens were still generated.
        """
        add_tokens = int(tokens or 0)
        add_turns = int(turns or 0)
        add_rounds = int(rounds or 0)
        if add_tokens < 0 or add_turns < 0 or add_rounds < 0:
            logger.debug("council scheduler %s: negative spend clamped "
                         "(tokens=%s turns=%s rounds=%s)",
                         self.session_id, add_tokens, add_turns, add_rounds)
        with self._guard:
            self._tokens += max(0, add_tokens)
            self._turns += max(0, add_turns)
            self._rounds += max(0, add_rounds)
        return self.state()

    def _exhausted(self, rounds: int, turns: int, wall: float, tokens: int) -> str:
        """The name of the first limit in `LIMIT_NAMES` that is out, or `""`.

        A limit of zero is a real instruction and not an unset field: a budget
        of zero rounds is a room that has been told not to start one, and
        `max_parallel` of zero is a room told to stop starting anything at all
        (`contracts.CouncilBudgets` says so).  In-flight work reaching the
        parallel ceiling is NOT exhaustion — that is backpressure, and
        `reserve()` waits it out.
        """
        if rounds >= int(self.budgets.max_rounds):
            return "max_rounds"
        if turns >= int(self.budgets.max_turns):
            return "max_turns"
        if wall >= float(self.budgets.max_wall_seconds):
            return "max_wall_seconds"
        if tokens >= int(self.budgets.max_total_tokens):
            return "max_total_tokens"
        if self.max_parallel <= 0:
            return "max_parallel"
        return ""

    def state(self) -> BudgetState:
        """What has been spent, and which limit (if any) that used up."""
        with self._guard:
            rounds, turns, tokens = self._rounds, self._turns, self._tokens
            in_flight = self._in_flight
        wall = max(0.0, float(self._clock()) - self._started)
        return BudgetState(
            rounds=rounds, turns=turns, wall_seconds=round(wall, 3),
            total_tokens=tokens, parallel=in_flight,
            exhausted=self._exhausted(rounds, turns, wall, tokens),
        )

    def can_start(self) -> Tuple[bool, str]:
        """`(True, "")`, or `(False, why)` naming the limit and both numbers.

        The sentence is for a human and for a log line; `stop_reason()` is the
        machine-readable half.  They are separate because a reason a policy
        routes on must come from a closed vocabulary, and a reason a person
        reads must contain the numbers.
        """
        current = self.state()
        limit = current.exhausted
        if not limit:
            return True, ""
        spent: Dict[str, Any] = {
            "max_rounds": current.rounds,
            "max_turns": current.turns,
            "max_wall_seconds": current.wall_seconds,
            "max_total_tokens": current.total_tokens,
            "max_parallel": self.max_parallel,
        }
        ceiling = getattr(self.budgets, limit, "?")
        if limit == "max_parallel":
            return False, ("max_parallel is 0: this room has been told to start "
                           "nothing further")
        return False, f"{limit}: {spent.get(limit)} of {ceiling} spent"

    def stop_reason(self) -> str:
        """One of `contracts.STOP_REASONS`, or `""` while the room may go on.

        `max_rounds` and `max_turns` keep their own names; wall clock and
        tokens are `budget_exhausted`.  A single generic reason would make
        "we said all there was to say in four rounds" and "we ran out of
        tokens mid-sentence" the same event in the ledger, and they are not.
        """
        return _STOP_FOR.get(self.state().exhausted, "")

    def waited_ms(self, participant_id: str) -> int:
        """Total milliseconds this participant has spent queueing in this room,
        including waits that ended in a timeout — a wait that bought nothing is
        exactly the one worth seeing (section 21)."""
        with self._guard:
            return int(self._waited.get(str(participant_id or ""), 0))

    def stats(self) -> Dict[str, Any]:
        """Everything section 21 asks this module to be able to say."""
        current = self.state()
        with self._guard:
            waits = dict(self._waited)
            granted, released = self._granted, self._released
            timeouts, refused = self._timeouts, self._refused
            in_flight, peak = self._in_flight, self._peak
            models = sorted({held.model for held in self._held.values()})
        return {
            "session_id": self.session_id,
            "budgets": self.budgets.to_dict(),
            "max_parallel": self.max_parallel,
            "state": current.to_dict(),
            "stop_reason": _STOP_FOR.get(current.exhausted, ""),
            "in_flight": in_flight,
            "in_flight_models": models,
            "peak_parallel": peak,
            "granted": granted,
            "released": released,
            "timeouts": timeouts,
            "refused": refused,
            "waited_ms": waits,
            "waited_ms_total": sum(waits.values()),
            "acquisition_order": list(ACQUISITION_ORDER),
        }


# ── one scheduler per room ─────────────────────────────────────────────────
#
# A process-wide registry, guarded by a plain `threading.Lock`, because a
# session's spend has to be the same number whether it is read by the turn, by
# a route or by the observability pass.  Two schedulers for one room would be
# two half-budgets, and the room would spend both.

_SCHEDULERS: Dict[str, CouncilScheduler] = {}
_REGISTRY_GUARD = threading.Lock()


def scheduler_for(session: Any) -> CouncilScheduler:
    """The scheduler for this session, made once and kept.

    Accepts a `CouncilSession` (or anything with `.id` and `.budgets`) and, for
    convenience at a call site that only has one, a bare session id.

    A session whose budgets were edited keeps the scheduler it already has, and
    the difference is logged.  Rebuilding it would reset the counters, which
    means editing a budget would erase the bill — the one thing a budget must
    never let you do.  Replacing a scheduler on purpose is `reset_schedulers()`
    plus a new call.
    """
    if isinstance(session, str):
        session_id, budgets, wanted = session, None, None
    else:
        session_id = str(getattr(session, "id", "") or "")
        budgets = getattr(session, "budgets", None)
        wanted = budgets
    session_id = session_id or "council_unknown"
    with _REGISTRY_GUARD:
        existing = _SCHEDULERS.get(session_id)
        if existing is not None:
            if wanted is not None and isinstance(wanted, CouncilBudgets) \
                    and wanted != existing.budgets:
                logger.info(
                    "council scheduler %s: keeping the running scheduler; its "
                    "budgets differ from the session's (%s vs %s) and a rebuild "
                    "would reset what the room has already spent",
                    session_id, existing.budgets.to_dict(), wanted.to_dict())
            return existing
        parsed = _as_budgets(budgets)
        made = CouncilScheduler(session_id, budgets=parsed,
                                max_parallel=int(parsed.max_parallel))
        _SCHEDULERS[session_id] = made
        logger.debug("council scheduler %s: created with %s",
                     session_id, parsed.to_dict())
        return made


def reset_schedulers() -> None:
    """Forget every scheduler.  For tests and for a process that is shutting a
    council subsystem down; it releases nothing, because a reservation still in
    flight belongs to the coroutine holding it and not to this table."""
    with _REGISTRY_GUARD:
        count = len(_SCHEDULERS)
        _SCHEDULERS.clear()
    _FALLBACK_LOCKS.clear()
    if count:
        logger.debug("council scheduler: forgot %d scheduler(s)", count)


# A name in `_STOP_FOR` that is not in `STOP_REASONS` would reach the ledger as
# a string nothing routes on.  The test suite asserts this; the log line is for
# the case where somebody edits one tuple and ships before running it.
_UNROUTABLE = sorted({r for r in _STOP_FOR.values() if r not in STOP_REASONS})
if _UNROUTABLE:  # pragma: no cover - guarded by a test
    logger.error("council scheduler: %s are not in STOP_REASONS %s; a stop "
                 "reason nothing routes on is a string in a log",
                 _UNROUTABLE, list(STOP_REASONS))
