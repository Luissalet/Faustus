"""L66/HW-01 — admission is a real atomic reservation, not two independent
readings of the same free memory.

The night of 08-09-2026 (src/vram_admission.py's own module docstring): two
27B models were admitted within the same instant because each `assess()`
call read the same "fits" answer before either had actually loaded. This
module pins two things the backlog's HW-01 entry asks for explicitly:

  1. `try_reserve` (already built in an earlier wave) really is atomic under
     genuine multi-thread concurrency, and `admit()` really uses it — two
     `admit()` calls racing for the same budget never both walk away
     believing they reserved the same bytes.
  2. a model whose KV cost was never measured is labeled `estimate: "minimum"`
     (never silently presented as a real number), and a measured one is
     labeled `estimate: "measured"`.
"""
from __future__ import annotations

import asyncio
import threading

import pytest

from src import vram_admission as va
from src import vram_fit

GIB = 2**30
ROOT = "http://127.0.0.1:11434"
EP = ROOT + "/v1/chat/completions"

Q8 = {"name": "qwen3.8:27b-q8_0", "size": 29 * GIB, "digest": "d-q8"}
Q4 = {"name": "qwen3.8:27b-q4_K_M", "size": 17 * GIB, "digest": "d-q4"}


@pytest.fixture(autouse=True)
def clean_tables():
    vram_fit.KV_RATES.clear()
    va._PENDING.clear()
    va._RESERVATIONS.clear()
    yield
    vram_fit.KV_RATES.clear()
    va._PENDING.clear()
    va._RESERVATIONS.clear()


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


# ── 1. try_reserve is atomic under REAL thread concurrency ─────────────────


def test_try_reserve_is_atomic_under_concurrent_threads():
    """Two threads race to reserve more than the budget allows, released at
    exactly the same moment via a barrier so the race is real, not just
    theoretically possible. If the read-then-write in try_reserve were not
    under one lock, both could observe "room" and both would succeed —
    exactly the QA-24 failure this function exists to close."""
    budget = 10 * GIB
    need = 6 * GIB  # two of these together exceed the budget; one alone fits
    results: list = [None, None]
    barrier = threading.Barrier(2)

    def worker(i):
        barrier.wait()
        results[i] = va.try_reserve(ROOT, f"model-{i}", need, budget)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    succeeded = [r for r in results if r is not None]
    assert len(succeeded) == 1, f"exactly one reservation must win the shared budget, got {results}"
    # The bytes actually held never exceed the budget that was granted.
    assert va.reserved_bytes(ROOT) == need


# ── 2. admit() end-to-end: two concurrent admits, one budget ───────────────


def test_two_concurrent_admits_do_not_receive_the_same_budget(monkeypatch):
    """Card has room for exactly one of two different 17 GB-class models
    alongside nothing else resident. Two admit() calls launched together
    (asyncio.gather, mode='auto' so neither blocks on a person) must not
    both walk away believing they reserved the same free bytes: the second
    one's own assess() sees its reservation attempt refused and is treated
    exactly like "does not fit", not silently granted the same room."""
    other_model = {"name": "qwen3.5:9b-q4", "size": 17 * GIB, "digest": "d-other"}
    _ollama(monkeypatch, tags=[Q4, other_model], ps=[], vram=_card(20, 0))
    # 20 GB total minus reserve/headroom leaves room for one 17 GB-class model
    # (weights-only headroom is 1.5 GB), not two.

    events_a: list = []
    events_b: list = []

    async def _run():
        return await asyncio.gather(
            va.admit(EP, Q4["name"], mode="auto", on_progress=events_a.append),
            va.admit(EP, other_model["name"], mode="auto", on_progress=events_b.append),
        )

    results = asyncio.run(_run())

    # Both calls return "proceed" (mode=auto never blocks on a person even
    # when nothing can be unloaded) — but they must not have gotten there by
    # both believing they held the same reservation at the same time.
    assert results == ["proceed", "proceed"]
    warned = [e for events in (events_a, events_b) for e in events
              if e.get("phase") == "warning" and "does not fit" in e.get("message", "")]
    # Exactly one of the two lost the race for the shared room and was told
    # so; the winner never sees this warning for itself.
    assert len(warned) == 1, f"expected exactly one loser of the reservation race, got: {events_a + events_b}"
    # No reservation is left dangling once both calls have returned — the
    # loser never got the room in the first place, and the winner's model
    # was never observed resident (this test never actually loads anything),
    # so its reservation is still legitimately held, not doubled up.
    assert len(va.reservations_snapshot()) == 1


# ── 3. KV-unknown models are labeled a floor, not a fabricated number ──────


def test_unmeasured_kv_is_labeled_a_minimum_not_a_measurement(monkeypatch):
    _ollama(monkeypatch, tags=[Q8, Q4],
            ps=[{"name": Q8["name"], "digest": "d-q8", "size": 33 * GIB,
                 "size_vram": 26 * GIB, "context_length": 65536}],
            vram=_card(28, 27))
    a = va.assess(ROOT, Q4["name"])  # Q4 was never loaded: no KV_RATES entry
    assert a["measured"] is False
    assert a["estimate"] == "minimum"


def test_measured_kv_is_labeled_measured(monkeypatch):
    vram_fit.remember_kv_rate("d-q4", per_token=100_000.0, ctx=65536)
    SMALL = {"name": "qwen3.5:9b", "size": 6 * GIB, "digest": "d-9b"}
    _ollama(monkeypatch, tags=[Q4, SMALL],
            ps=[{"name": SMALL["name"], "digest": "d-9b", "size": 7 * GIB, "size_vram": 7 * GIB,
                 "context_length": 8192}],
            vram=_card(28, 7.5))
    a = va.assess(ROOT, Q4["name"])
    assert a["measured"] is True
    assert a["estimate"] == "measured"
