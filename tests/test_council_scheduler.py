"""Tests for src/council/scheduler.py — the four rules in the plan's section 20.

    - two calls to the same model serialize;
    - distinct models overlap under the global limit;
    - the acquisition order does not deadlock model lock against GPU slot;
    - an exhausted budget prevents STARTING another call (and never kills one
      already in flight).

Everything is injected: the GPU semaphore, the per-model lock registry and the
clock. No test here waits on a real model, and none of them touch the shared
machine-wide semaphore — a unit test that queued on the real GPU slots would
be a unit test whose result depends on what else the machine is doing.
"""
from __future__ import annotations

import asyncio

import pytest

from src.council.contracts import STOP_REASONS, CouncilBudgets, CouncilSession
from src.council.scheduler import (
    ACQUISITION_ORDER,
    LIMIT_NAMES,
    NO_GPU_SLOTS,
    BudgetState,
    CouncilScheduler,
    Reservation,
    reset_schedulers,
    scheduler_for,
)

# Long enough that a wedged test fails instead of hanging the suite.
DEADLINE = 5.0


class _Clock:
    """A clock the test moves by hand, so a wall-clock budget can run out
    without anybody sleeping."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


def _budgets(**kw) -> CouncilBudgets:
    base = {
        "max_rounds": 10, "max_turns": 50, "max_wall_seconds": 600,
        "max_total_tokens": 100_000, "max_parallel": 2,
    }
    base.update(kw)
    return CouncilBudgets(**base)


def _make(*, budgets=None, max_parallel=2, gpu=None, locks=None, clock=None,
          session_id="council_test") -> CouncilScheduler:
    """A scheduler wired to nothing shared.

    `model_locks={}` gives each scheduler its own registry (the mutable-mapping
    branch), and `NO_GPU_SLOTS` says "this room does not wait on the GPU",
    which is not the same as `None` ("ask tournament for the shared one").
    """
    budgets = budgets if budgets is not None else _budgets(max_parallel=max_parallel)
    return CouncilScheduler(
        session_id,
        budgets=budgets,
        max_parallel=max_parallel,
        gpu_slots=gpu if gpu is not None else NO_GPU_SLOTS,
        model_locks={} if locks is None else locks,
        clock=clock,
    )


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_schedulers()
    yield
    reset_schedulers()


# --- rule 1: same model serializes, distinct models overlap ----------------

async def test_two_calls_to_the_same_model_serialize():
    sched = _make(max_parallel=2)
    first = await sched.reserve("p_claude", "qwen3:32b")
    assert first is not None

    waiting = asyncio.create_task(sched.reserve("p_codex", "qwen3:32b", timeout_s=DEADLINE))
    await asyncio.sleep(0.05)
    assert not waiting.done(), "the second call to one model must wait for the first"
    assert sched.state().parallel == 1

    await sched.release(first)
    second = await asyncio.wait_for(waiting, timeout=DEADLINE)
    assert second is not None
    assert second.model == "qwen3:32b"
    assert sched.state().parallel == 1
    await sched.release(second)
    assert sched.state().parallel == 0


async def test_distinct_models_overlap_under_the_global_limit():
    sched = _make(max_parallel=2, gpu=asyncio.Semaphore(2))
    a = await sched.reserve("p_claude", "qwen3:32b")
    b = await sched.reserve("p_codex", "llama3:8b")
    assert a is not None and b is not None
    # Both held at the same moment: that is what "overlap" means here.
    assert sched.state().parallel == 2
    assert sched.stats()["peak_parallel"] == 2
    await sched.release(a)
    await sched.release(b)
    assert sched.state().parallel == 0


async def test_the_global_limit_is_the_ceiling_even_for_distinct_models():
    sched = _make(max_parallel=1, gpu=asyncio.Semaphore(4))
    a = await sched.reserve("p_claude", "qwen3:32b")
    assert a is not None
    waiting = asyncio.create_task(sched.reserve("p_codex", "llama3:8b", timeout_s=DEADLINE))
    await asyncio.sleep(0.05)
    assert not waiting.done(), "max_parallel=1 must hold the second model back"
    await sched.release(a)
    assert await asyncio.wait_for(waiting, timeout=DEADLINE) is not None


async def test_a_constructor_argument_may_narrow_parallelism_never_widen_it():
    sched = _make(budgets=_budgets(max_parallel=2), max_parallel=8)
    assert sched.max_parallel == 2
    tighter = _make(budgets=_budgets(max_parallel=4), max_parallel=1)
    assert tighter.max_parallel == 1


# --- rule 2: the acquisition order does not deadlock -----------------------

class _WatchingSlots:
    """A GPU semaphore that records whether a model lock was already held at
    the moment it was asked for.

    That is the whole invariant: nothing may hold a GPU slot while it waits for
    a model lock, which is what `tournament._Gate` documents and what this
    scheduler had to keep, because both wait on the SAME two objects in
    production.
    """

    def __init__(self, locks, permits: int) -> None:
        self._locks = locks
        self._sem = asyncio.Semaphore(permits)
        self.lock_held_first = []

    async def acquire(self) -> bool:
        self.lock_held_first.append(any(lock.locked() for lock in self._locks.values()))
        await self._sem.acquire()
        return True

    def release(self) -> None:
        self._sem.release()


async def test_the_model_lock_is_always_taken_before_the_gpu_slot():
    locks: dict = {}
    slots = _WatchingSlots(locks, 2)
    sched = _make(max_parallel=2, gpu=slots, locks=locks)

    a = await sched.reserve("p_claude", "qwen3:32b")
    b = await sched.reserve("p_codex", "llama3:8b")
    await sched.release(a)
    await sched.release(b)

    assert slots.lock_held_first == [True, True]
    assert ACQUISITION_ORDER == ("parallel", "model_lock", "gpu_slot")


async def _crossed(sched: CouncilScheduler, pid: str, first: str, second: str) -> None:
    one = await sched.reserve(pid, first, timeout_s=DEADLINE)
    assert one is not None
    await asyncio.sleep(0.01)          # hold it long enough to actually cross
    await sched.release(one)
    two = await sched.reserve(pid, second, timeout_s=DEADLINE)
    assert two is not None
    await sched.release(two)


async def test_two_crossed_participants_both_finish():
    """One GPU slot, two participants wanting the two models in opposite
    orders. With the slot taken before the model lock this is the classic
    hold-and-wait cycle; taken after, both finish."""
    locks: dict = {}
    sched = _make(max_parallel=2, gpu=asyncio.Semaphore(1), locks=locks)
    await asyncio.wait_for(
        asyncio.gather(
            _crossed(sched, "p_claude", "qwen3:32b", "llama3:8b"),
            _crossed(sched, "p_codex", "llama3:8b", "qwen3:32b"),
        ),
        timeout=DEADLINE,
    )
    assert sched.state().parallel == 0
    assert sched.stats()["granted"] == 4
    assert sched.stats()["released"] == 4


# --- rule 3: an exhausted budget prevents starting another call ------------

async def test_rounds_exhausted_names_max_rounds():
    sched = _make(budgets=_budgets(max_rounds=1))
    assert sched.can_start() == (True, "")
    state = sched.spend(rounds=1)
    assert state.exhausted == "max_rounds"
    allowed, why = sched.can_start()
    assert allowed is False
    assert "max_rounds" in why and "1 of 1" in why
    assert sched.stop_reason() == "max_rounds"


async def test_turns_exhausted_names_max_turns():
    sched = _make(budgets=_budgets(max_turns=2))
    sched.spend(turns=2)
    assert sched.state().exhausted == "max_turns"
    assert sched.can_start()[0] is False
    assert sched.stop_reason() == "max_turns"


async def test_tokens_exhausted_is_budget_exhausted():
    sched = _make(budgets=_budgets(max_total_tokens=100))
    sched.spend(tokens=60)
    assert sched.stop_reason() == ""
    sched.spend(tokens=40)
    assert sched.state().exhausted == "max_total_tokens"
    assert sched.stop_reason() == "budget_exhausted"


async def test_wall_clock_exhausted_is_budget_exhausted():
    clock = _Clock()
    sched = _make(budgets=_budgets(max_wall_seconds=60), clock=clock)
    clock.advance(59)
    assert sched.stop_reason() == ""
    clock.advance(1)
    assert sched.state().exhausted == "max_wall_seconds"
    assert sched.stop_reason() == "budget_exhausted"


async def test_zero_parallel_means_start_nothing_further():
    sched = _make(budgets=_budgets(max_parallel=0), max_parallel=0)
    allowed, why = sched.can_start()
    assert allowed is False
    assert "max_parallel" in why
    assert sched.stop_reason() == "budget_exhausted"
    assert await sched.reserve("p_claude", "qwen3:32b", timeout_s=DEADLINE) is None


async def test_every_stop_reason_comes_from_the_closed_vocabulary():
    for limit in LIMIT_NAMES:
        sched = _make(budgets=_budgets(**{limit: 0}), max_parallel=0 if limit == "max_parallel" else 2)
        reason = sched.stop_reason()
        assert reason, f"{limit} at zero must stop the room"
        assert reason in STOP_REASONS


async def test_an_exhausted_budget_refuses_a_new_reservation():
    sched = _make(budgets=_budgets(max_turns=1))
    sched.spend(turns=1)
    assert await sched.reserve("p_claude", "qwen3:32b", timeout_s=DEADLINE) is None
    assert sched.stats()["refused"] == 1
    assert sched.stats()["granted"] == 0


# --- rule 4: a call in flight is not killed by exhaustion ------------------

async def test_exhausting_the_budget_does_not_kill_a_call_in_flight():
    sched = _make(budgets=_budgets(max_total_tokens=100))
    held = await sched.reserve("p_claude", "qwen3:32b")
    assert held is not None

    sched.spend(tokens=100)                       # the budget runs out mid-call
    assert sched.stop_reason() == "budget_exhausted"
    assert sched.state().parallel == 1, "the reservation already granted stands"
    assert await sched.reserve("p_codex", "llama3:8b", timeout_s=DEADLINE) is None

    await sched.release(held)                     # and it can still be given back
    assert sched.state().parallel == 0
    assert sched.stats()["released"] == 1


# --- rule 6: a timeout answers None instead of waiting forever -------------

async def test_reserve_times_out_instead_of_waiting_forever():
    sched = _make(max_parallel=2)
    held = await sched.reserve("p_claude", "qwen3:32b")
    assert held is not None

    late = await sched.reserve("p_codex", "qwen3:32b", timeout_s=0.05)
    assert late is None
    assert sched.stats()["timeouts"] == 1
    # A wait that bought nothing is still time this participant spent queueing.
    assert sched.waited_ms("p_codex") >= 0
    assert "p_codex" in sched.stats()["waited_ms"]

    # And the abandoned attempt gave everything back: the model is free again.
    await sched.release(held)
    again = await sched.reserve("p_codex", "qwen3:32b", timeout_s=DEADLINE)
    assert again is not None
    await sched.release(again)


async def test_a_zero_timeout_takes_only_what_is_free_right_now():
    sched = _make(max_parallel=2)
    held = await sched.reserve("p_claude", "qwen3:32b")
    assert await sched.reserve("p_codex", "qwen3:32b", timeout_s=0) is None
    await sched.release(held)


# --- the small guarantees around them --------------------------------------

async def test_release_is_idempotent_and_forgiving():
    sched = _make()
    held = await sched.reserve("p_claude", "qwen3:32b")
    await sched.release(held)
    await sched.release(held)                     # no raise, no double release
    await sched.release(None)                     # nor for something never granted
    assert sched.state().parallel == 0
    assert sched.stats()["released"] == 1


async def test_model_locks_may_be_a_callable():
    locks: dict = {}

    def factory(model: str) -> asyncio.Lock:
        return locks.setdefault(model, asyncio.Lock())

    sched = _make(locks=factory)
    held = await sched.reserve("p_claude", "qwen3:32b")
    assert held is not None
    assert locks["qwen3:32b"].locked()
    await sched.release(held)
    assert not locks["qwen3:32b"].locked()


async def test_reservation_and_budget_state_round_trip_to_dicts():
    clock = _Clock()
    sched = _make(clock=clock)
    held = await sched.reserve("p_claude", "qwen3:32b")
    assert isinstance(held, Reservation)
    assert set(held.to_dict()) == {"participant_id", "model", "token",
                                   "acquired_at", "waited_ms"}
    clock.advance(3.5)
    sched.spend(tokens=120, turns=1, rounds=1)
    state = sched.state()
    assert isinstance(state, BudgetState)
    assert state.to_dict() == {
        "rounds": 1, "turns": 1, "wall_seconds": 3.5, "total_tokens": 120,
        "parallel": 1, "exhausted": "",
    }
    await sched.release(held)


async def test_spend_clamps_a_negative_amount_rather_than_refunding():
    sched = _make()
    sched.spend(tokens=50)
    state = sched.spend(tokens=-40)
    assert state.total_tokens == 50


async def test_waited_ms_accumulates_per_participant():
    clock = _Clock()
    sched = _make(max_parallel=2, clock=clock)
    first = await sched.reserve("p_claude", "qwen3:32b")

    async def _second():
        return await sched.reserve("p_codex", "qwen3:32b", timeout_s=DEADLINE)

    waiting = asyncio.create_task(_second())
    await asyncio.sleep(0.05)
    clock.advance(2.0)                            # the wait, as the injected clock sees it
    await sched.release(first)
    second = await asyncio.wait_for(waiting, timeout=DEADLINE)
    assert second is not None
    assert second.waited_ms == 2000
    assert sched.waited_ms("p_codex") == 2000
    assert sched.waited_ms("nobody") == 0
    await sched.release(second)


# --- one scheduler per room ------------------------------------------------

async def test_scheduler_for_is_one_per_session_and_takes_its_budgets():
    session = CouncilSession(id="council_abc",
                             budgets=_budgets(max_parallel=3, max_rounds=7))
    sched = scheduler_for(session)
    assert sched.session_id == "council_abc"
    assert sched.max_parallel == 3, "the session's own budget, not the default 2"
    assert sched.budgets.max_rounds == 7
    assert scheduler_for(session) is sched


async def test_a_changed_budget_does_not_reset_what_the_room_already_spent():
    session = CouncilSession(id="council_abc", budgets=_budgets(max_rounds=7))
    sched = scheduler_for(session)
    sched.spend(rounds=3, tokens=900)

    edited = CouncilSession(id="council_abc", budgets=_budgets(max_rounds=99))
    again = scheduler_for(edited)
    assert again is sched
    assert again.state().rounds == 3
    assert again.state().total_tokens == 900


async def test_reset_schedulers_forgets_them():
    sched = scheduler_for(CouncilSession(id="council_abc", budgets=_budgets()))
    reset_schedulers()
    assert scheduler_for(CouncilSession(id="council_abc", budgets=_budgets())) is not sched


async def test_scheduler_for_accepts_a_bare_session_id():
    sched = scheduler_for("council_plain")
    assert sched.session_id == "council_plain"
    assert scheduler_for("council_plain") is sched
