"""HW-01 / QA-24 · Atomic VRAM reservations under concurrent admission.

The night of 08-09-2026 two 27B models were admitted within the same instant:
`assess()`/`open_ticket()` read a fresh /api/ps + nvidia-smi snapshot each
time, and two jobs a few milliseconds apart both read the same free memory
before either had actually loaded anything — neither read was wrong, but
together they promised the same bytes twice.

`src.vram_admission.try_reserve()` closes that window with a lock-guarded
test-and-set: `assess()` now nets active reservations out of the budget it
reports, and `admit()` atomically reserves the footprint of a model it is
about to say "proceed" for, so a second concurrent `admit()` sees a smaller
budget and — if that budget no longer covers it — is routed through the same
"does not fit" negotiation (`ask`: opens a ticket and waits; `auto`: unloads
or spills) instead of racing the first for the same memory.
"""
from __future__ import annotations

import asyncio

import pytest

from src import vram_admission as va

GIB = 2**30
ROOT = "http://127.0.0.1:11434"
EP = ROOT + "/v1/chat/completions"


@pytest.fixture(autouse=True)
def clean_reservations():
    va._RESERVATIONS.clear()
    va._PENDING.clear()
    yield
    va._RESERVATIONS.clear()
    va._PENDING.clear()


# ── the primitive: try_reserve/release/expiry ───────────────────────────────

def test_try_reserve_is_a_lock_guarded_test_and_set():
    """Two reservations that together exceed the budget: the first claims the
    room, the second is refused outright — never both."""
    first = va.try_reserve(ROOT, "model-a", 18 * GIB, 24 * GIB)
    assert first is not None
    second = va.try_reserve(ROOT, "model-b", 18 * GIB, 24 * GIB)
    assert second is None  # 18 + 18 > 24: no room left for both
    assert va.reserved_bytes(ROOT) == 18 * GIB


def test_try_reserve_admits_a_second_reservation_that_still_fits():
    first = va.try_reserve(ROOT, "model-a", 10 * GIB, 24 * GIB)
    second = va.try_reserve(ROOT, "model-b", 10 * GIB, 24 * GIB)
    assert first is not None and second is not None
    assert va.reserved_bytes(ROOT) == 20 * GIB


def test_release_frees_the_room_for_the_next_reservation():
    rid = va.try_reserve(ROOT, "model-a", 18 * GIB, 24 * GIB)
    assert va.try_reserve(ROOT, "model-b", 18 * GIB, 24 * GIB) is None
    va.release_reservation(rid)
    assert va.try_reserve(ROOT, "model-b", 18 * GIB, 24 * GIB) is not None


def test_release_reservations_for_model_matches_by_name_case_insensitively():
    va.try_reserve(ROOT, "Qwen3.5:9b", 5 * GIB, 24 * GIB)
    assert va.reserved_bytes(ROOT) == 5 * GIB
    va.release_reservations_for_model(ROOT, "qwen3.5:9B")
    assert va.reserved_bytes(ROOT) == 0


def test_reservations_expire_on_their_own(monkeypatch):
    """"o caduca": a reservation nobody released still frees itself after the
    ttl, so a crashed loader cannot starve the budget forever."""
    now = [1000.0]
    monkeypatch.setattr(va.time, "time", lambda: now[0])
    va.try_reserve(ROOT, "model-a", 18 * GIB, 24 * GIB, ttl=30.0)
    assert va.reserved_bytes(ROOT) == 18 * GIB
    now[0] += 31.0
    assert va.reserved_bytes(ROOT) == 0  # swept lazily on the next read


def test_reservations_are_scoped_per_device_and_globally():
    """"por dispositivo (GPU) y global": a reservation against one card does
    not eat into another card's budget, or the pool-wide (device=None) one."""
    va.try_reserve(ROOT, "model-a", 18 * GIB, 24 * GIB, device=0)
    assert va.reserved_bytes(ROOT, device=0) == 18 * GIB
    assert va.reserved_bytes(ROOT, device=1) == 0
    assert va.reserved_bytes(ROOT) == 0  # the pool-wide key is a different bucket
    assert va.try_reserve(ROOT, "model-b", 18 * GIB, 24 * GIB, device=1) is not None


# ── assess() nets reservations out of the budget it reports ────────────────

def _card(total_gb: float, used_gb: float):
    return {"supported": True, "name": "GeForce", "total": int(total_gb * GIB),
            "used": int(used_gb * GIB), "free": int((total_gb - used_gb) * GIB),
            "gpus": [{"index": 0, "name": "GeForce", "uuid": "u0", "total": int(total_gb * GIB),
                      "used": int(used_gb * GIB), "free": int((total_gb - used_gb) * GIB)}]}


def _ollama(monkeypatch, *, tags, ps, vram):
    def _get(root, path, timeout):
        return {"models": tags} if path == "/api/tags" else {"models": ps}
    monkeypatch.setattr(va, "_get", _get)
    monkeypatch.setattr("src.gpu_shared_memory.vram_snapshot", lambda: vram)
    monkeypatch.setattr("src.gpu_placement.placement", lambda root, loaded, gpus: {})


BIG_A = {"name": "big-a:27b", "size": 18 * GIB, "digest": "d-a"}
BIG_B = {"name": "big-b:27b", "size": 18 * GIB, "digest": "d-b"}


def test_assess_reports_a_smaller_budget_once_something_is_reserved(monkeypatch):
    _ollama(monkeypatch, tags=[BIG_A, BIG_B], ps=[], vram=_card(24, 0.5))
    before = va.assess(ROOT, BIG_A["name"])
    assert before["fits"] is True

    va.try_reserve(ROOT, "someone-elses-model", 10 * GIB, before["budget_alongside_bytes"])
    after = va.assess(ROOT, BIG_A["name"])
    assert after["reserved_bytes"] == 10 * GIB
    assert after["budget_alongside_bytes"] == before["budget_alongside_bytes"] - 10 * GIB


# ── admit(): never promises the same memory to two concurrent jobs ─────────

def test_two_concurrent_admits_never_both_proceed_for_more_than_the_budget(monkeypatch):
    """The QA-24 scenario itself: two jobs ask to load, at the same instant,
    two models that individually fit the empty card but not both together.
    Exactly one proceeds; the other is routed into the normal "does not fit"
    negotiation (mode="ask" here) instead of being waved through alongside
    the first — a ticket is opened for it, i.e. it is left *waiting*, not
    admitted and not silently dropped either."""
    # 24 GiB card, ~0.8 GiB reserve; each model needs ~18.5 GiB (weights +
    # weights-only headroom) — one fits alongside the reserve, two do not.
    _ollama(monkeypatch, tags=[BIG_A, BIG_B], ps=[], vram=_card(24, 0.2))

    async def run():
        return await asyncio.gather(
            va.admit(EP, BIG_A["name"], owner="a", mode="ask", timeout=0.2),
            va.admit(EP, BIG_B["name"], owner="b", mode="ask", timeout=0.2),
            return_exceptions=True,
        )

    results = asyncio.run(run())
    proceeded = [r for r in results if r == "proceed"]
    cancelled = [r for r in results if isinstance(r, va.AdmissionCancelled)]
    # Never both: the defect this test pins is two "proceed"s for memory that
    # only exists once.
    assert len(proceeded) == 1, results
    assert len(cancelled) == 1, results
    # The loser was never silently admitted — it left a real, visible "no
    # answer in time" refusal, i.e. it was genuinely left waiting for someone
    # to decide, not fast-tracked through.
    assert "nobody chose" in str(cancelled[0])
