"""INF-05 Lote B1/B2: reservations that survive a live load, endpoint
aliasing, and the engine-agnostic `admit_bytes` gate for Cookbook serve.

Same "never a real nvidia-smi/network call in tests" rule as
`tests/test_vram_admission.py` — every GPU/Ollama reading here is a fake
patched onto the exact functions those modules expose.
"""
from __future__ import annotations

import asyncio

import pytest

from src import vram_admission as va
from src.contracts.base import now_iso
from src.contracts.inference import HardwareSnapshot

GIB = 2**30
ROOT = "http://127.0.0.1:11434"


@pytest.fixture(autouse=True)
def clean_tables():
    va._RESERVATIONS.clear()
    va._PENDING.clear()
    va._PINS.clear()
    va._LAST_ACTIVE.clear()
    yield
    va._RESERVATIONS.clear()
    va._PENDING.clear()
    va._PINS.clear()
    va._LAST_ACTIVE.clear()


# ── B1: heartbeat / mark_loading / hard cap ─────────────────────────────────

def test_heartbeat_renews_the_clock_and_reports_when_gone():
    rid = va.try_reserve(ROOT, "model-a", 10 * GIB, 24 * GIB, ttl=5.0)
    assert va.heartbeat(rid) is True
    assert va.heartbeat("rsv-does-not-exist") is False
    va.release_reservation(rid)
    assert va.heartbeat(rid) is False


def test_a_loading_reservation_never_expires_on_ttl_alone(monkeypatch):
    """§12/T12: "una reserva marcada loading nunca caduca antes del hard
    cap" — a short TTL alone must not sweep it once `mark_loading` is set."""
    now = [1000.0]
    monkeypatch.setattr(va.time, "time", lambda: now[0])
    rid = va.try_reserve(ROOT, "model-a", 10 * GIB, 24 * GIB, ttl=5.0)
    assert va.mark_loading(rid) is True
    now[0] += 60.0  # far past the 5s ttl
    assert va.reserved_bytes(ROOT) == 10 * GIB  # still held: it is "loading"


def test_a_loading_reservation_is_force_released_past_the_hard_cap(monkeypatch, caplog):
    now = [1000.0]
    monkeypatch.setattr(va.time, "time", lambda: now[0])
    rid = va.try_reserve(ROOT, "model-a", 10 * GIB, 24 * GIB, ttl=5.0)
    va.mark_loading(rid)
    now[0] += va.HARD_CAP_SECONDS + 1.0
    assert va.reserved_bytes(ROOT) == 0
    assert va.heartbeat(rid) is False


@pytest.mark.asyncio
async def test_heartbeat_while_loading_keeps_renewing_until_released(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(va.time, "time", lambda: now[0])
    rid = va.try_reserve(ROOT, "model-a", 10 * GIB, 24 * GIB, ttl=2.0)
    va.mark_loading(rid)

    # A fake clock plus a tiny sleep patch so the background task's own
    # `asyncio.sleep(interval)` advances the SAME fake clock instantly,
    # instead of a real-time wait — this test must run in milliseconds.
    real_sleep = asyncio.sleep

    async def _fast_sleep(_seconds):
        now[0] += va.HARD_CAP_SECONDS  # release() below ends the loop next tick
        await real_sleep(0)

    monkeypatch.setattr(va.asyncio, "sleep", _fast_sleep)
    task = asyncio.create_task(va.heartbeat_while_loading(rid, interval=1.0))
    await real_sleep(0.01)
    assert task.done()  # released itself once heartbeat() reported "gone"


def test_heartbeat_while_loading_is_a_noop_for_no_reservation():
    asyncio.run(va.heartbeat_while_loading(None))  # must not raise


# ── B1: endpoint aliasing (T14) ─────────────────────────────────────────────

def test_aliased_endpoints_share_reservations():
    rid = va.try_reserve("http://localhost:11434", "model-a", 10 * GIB, 24 * GIB)
    assert rid is not None
    # Asked through a DIFFERENT alias of the same loopback Ollama:
    assert va.reserved_bytes(ROOT) == 10 * GIB
    assert va.reserved_bytes("http://0.0.0.0:11434") == 10 * GIB
    # ollama_root() itself is untouched — still returns what was given.
    assert va.ollama_root("http://localhost:11434/v1") == "http://localhost:11434"


def test_aliased_endpoints_share_pins_and_last_active():
    va.pin_model("http://localhost:11434", "big:27b")
    assert va.is_pinned(ROOT, "big:27b") is True
    assert va.pinned_models("http://0.0.0.0:11434") == ["big:27b"]
    va.unpin_model(ROOT, "big:27b")
    assert va.is_pinned("http://localhost:11434", "big:27b") is False


def test_aliased_endpoints_release_each_other_s_reservations():
    va.try_reserve("http://localhost:11434", "qwen3.5:9b", 5 * GIB, 24 * GIB)
    assert va.reserved_bytes(ROOT) == 5 * GIB
    va.release_reservations_for_model(ROOT, "qwen3.5:9b")
    assert va.reserved_bytes("http://localhost:11434") == 0


# ── B1: reservations by physical device (uuid, not just index) ─────────────

def test_try_reserve_accepts_a_uuid_device():
    rid = va.try_reserve(ROOT, "model-a", 10 * GIB, 24 * GIB, device="GPU-1234-uuid")
    assert rid is not None
    assert va.reserved_bytes(ROOT, device="GPU-1234-uuid") == 10 * GIB
    assert va.reserved_bytes(ROOT, device="GPU-9999-other") == 0
    assert va.reserved_bytes(ROOT) == 0  # the pool bucket is untouched


def test_reserved_bytes_include_devices_folds_devices_into_the_pool_query():
    va.try_reserve(ROOT, "model-a", 10 * GIB, 24 * GIB, device=0)
    va.try_reserve(ROOT, "model-b", 4 * GIB, 24 * GIB)  # pool-level
    assert va.reserved_bytes(ROOT) == 4 * GIB  # unchanged default behaviour
    assert va.reserved_bytes(ROOT, include_devices=True) == 14 * GIB


# ── B2: admit_bytes — proceed / blocked / unknown / off ─────────────────────

def _fake_budget(total_gb: float, used_gb: float, *, stale: bool = False):
    def _budget(gpu_indices):
        return {"budget_bytes": int((total_gb - used_gb) * GIB), "total_bytes": int(total_gb * GIB),
               "used_bytes": int(used_gb * GIB), "stale": stale, "source": "observed",
               "gpu_count": 1, "gpu_name": "GeForce", "reason": ""}
    return _budget


def test_admit_bytes_with_no_weights_is_unknown_and_never_blocks():
    result = asyncio.run(va.admit_bytes(label="org/model", bytes_needed=None, gpu_indices=[]))
    assert result["decision"] == "unknown"
    assert result["ticket"] is None
    assert "weights size unknown" in result["assessment"]["reason"]


def test_admit_bytes_off_mode_checks_nothing():
    result = asyncio.run(va.admit_bytes(label="org/model", bytes_needed=20 * GIB, gpu_indices=[], mode="off"))
    assert result["decision"] == "off"


def test_admit_bytes_proceeds_and_reserves_when_it_fits(monkeypatch):
    monkeypatch.setattr(va, "_physical_budget_for", _fake_budget(24, 1))
    result = asyncio.run(va.admit_bytes(label="org/model", bytes_needed=10 * GIB, gpu_indices=[0], mode="ask"))
    assert result["decision"] == "proceed"
    assert result["reservation_id"] is not None
    assert result["assessment"]["fits"] is True


def test_admit_bytes_blocks_with_a_ticket_when_it_does_not_fit(monkeypatch):
    monkeypatch.setattr(va, "_physical_budget_for", _fake_budget(24, 20))  # 4 GiB free
    result = asyncio.run(va.admit_bytes(label="org/big-model", bytes_needed=20 * GIB, gpu_indices=[0], mode="ask"))
    assert result["decision"] == "blocked"
    assert result["reservation_id"] is None
    ticket = va.get_ticket(result["ticket"])
    assert ticket is not None
    assert ticket.kind == "serve"
    assert ticket.assessment["fits"] is False
    assert ticket.assessment["shortfall_bytes"] > 0


def test_admit_bytes_is_unknown_when_the_budget_reading_is_stale(monkeypatch):
    monkeypatch.setattr(va, "_physical_budget_for", _fake_budget(24, 1, stale=True))
    result = asyncio.run(va.admit_bytes(label="org/model", bytes_needed=10 * GIB, gpu_indices=[0]))
    assert result["decision"] == "unknown"
    assert result["assessment"]["stale"] is True


def test_admit_bytes_is_unknown_when_there_is_no_gpu_reading_at_all(monkeypatch):
    monkeypatch.setattr(va, "_physical_budget_for", lambda gpu_indices: {
        "budget_bytes": 0, "total_bytes": None, "used_bytes": None, "stale": True,
        "source": "absent", "gpu_count": 0, "gpu_name": "", "reason": "no nvidia-smi",
    })
    result = asyncio.run(va.admit_bytes(label="org/model", bytes_needed=10 * GIB, gpu_indices=[]))
    assert result["decision"] == "unknown"
    assert result["assessment"]["reason"] == "no nvidia-smi"


# T13: two concurrent admit_bytes for the same bytes on the same GPU — only
# one may ever proceed for memory that only exists once.
def test_admit_bytes_two_concurrent_never_both_proceed(monkeypatch):
    monkeypatch.setattr(va, "_physical_budget_for", _fake_budget(24, 0.2))  # ~23.8 GiB free

    async def run():
        return await asyncio.gather(
            va.admit_bytes(label="org/model-a", bytes_needed=18 * GIB, gpu_indices=[0], mode="ask"),
            va.admit_bytes(label="org/model-b", bytes_needed=18 * GIB, gpu_indices=[0], mode="ask"),
        )

    results = asyncio.run(run())
    decisions = [r["decision"] for r in results]
    assert decisions.count("proceed") == 1, results
    assert decisions.count("blocked") == 1, results


# §12: only Ollama residents on the requested GPUs are ever offered for
# eviction — a foreign process never appears in `suggestion`.
def test_admit_bytes_suggestion_only_ever_names_ollama_residents(monkeypatch):
    monkeypatch.setattr(va, "_physical_budget_for", _fake_budget(24, 20))  # short by a lot

    def _fake_get(root, path, timeout):
        if path == "/api/ps":
            return {"models": [{"name": "qwen3.5:9b", "size_vram": 18 * GIB}]}
        return {"models": []}

    monkeypatch.setattr(va, "_get", _fake_get)
    monkeypatch.setattr("src.gpu_shared_memory.vram_snapshot", lambda: {"supported": False})
    monkeypatch.setattr(
        "src.gpu_placement.placement",
        lambda root, loaded, gpus: {"qwen3.5:9b": {"gpus": [0], "per_gpu": [], "placement": "single", "pid": 111}},
    )
    result = asyncio.run(va.admit_bytes(
        label="org/big-model", bytes_needed=20 * GIB, gpu_indices=[0], mode="ask",
        ollama_roots=(ROOT,),
    ))
    assert result["decision"] == "blocked"
    assessment = result["assessment"]
    assert assessment["suggestion"] == ["qwen3.5:9b"]
    # A foreign process (pid 4242, "ComfyUI") is never a candidate even
    # though it is on the same GPU and holds real bytes — it simply never
    # shows up in `/api/ps`, so it never reaches `suggestion` by construction.
    assert all("ComfyUI" not in n for n in assessment["suggestion"])
    assert assessment["residents"][0]["root"] == ROOT


def test_admit_bytes_reservation_root_is_separate_from_ollama_roots(monkeypatch):
    """A serve candidate's own reservation must not collide with (or be
    confused for) an Ollama model's reservation on the same physical GPU."""
    monkeypatch.setattr(va, "_physical_budget_for", _fake_budget(24, 1))
    asyncio.run(va.admit_bytes(label="org/model", bytes_needed=10 * GIB, gpu_indices=[0]))
    assert va.reserved_bytes(ROOT) == 0  # nothing landed under the Ollama root
    assert va.reserved_bytes(va._physical_reservation_root([0])) == 10 * GIB + va.HEADROOM_MEASURED


# ── B1: reconcile_on_start ───────────────────────────────────────────────────

def test_reconcile_on_start_clears_in_memory_state_and_logs():
    va.try_reserve(ROOT, "model-a", 5 * GIB, 24 * GIB)
    va.open_ticket(ROOT, "model-b", {"residents": []})
    result = va.reconcile_on_start()
    assert result == {"cleared": 1, "pending_cleared": 1}
    assert va.reservations_snapshot() == []
    assert va.pending() == []
