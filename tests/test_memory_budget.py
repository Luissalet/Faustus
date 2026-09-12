"""src/memory_budget.py — INF-05 §11 A3.

Pure arithmetic over fixtures — no `nvidia-smi`, no HTTP, no model load.
`system_memory()` is the one impure reading; it is exercised only for
"does not raise", never asserted against real numbers.
"""
from __future__ import annotations

import pytest

from src import memory_budget as mb
from src.contracts.inference import GpuInfo, HardwareSnapshot, LaunchReceipt, EngineIdentity

GIB = 1024 * 1024 * 1024


def _snapshot(*gpus, observed_at="2026-09-12T00:00:00Z") -> HardwareSnapshot:
    return HardwareSnapshot(host="box", gpus=tuple(gpus), topology="known", observed_at=observed_at)


def _vram(*gpus) -> dict:
    return {"supported": True, "name": "pool", "count": len(gpus),
           "total": sum(g["total"] for g in gpus), "used": sum(g["used"] for g in gpus),
           "gpus": [dict(g) for g in gpus]}


def _card(index, uuid, total_gb, used_gb, name="RTX"):
    return {"index": index, "name": name, "uuid": uuid,
           "total": int(total_gb * GIB), "used": int(used_gb * GIB),
           "free": int((total_gb - used_gb) * GIB)}


def _receipt(session_id, *, gpus_plan=None, weights_bytes=None) -> LaunchReceipt:
    plan = {}
    if gpus_plan is not None:
        plan["gpus"] = gpus_plan
    if weights_bytes is not None:
        plan["weights_bytes"] = weights_bytes
    return LaunchReceipt(
        session_id=session_id, engine=EngineIdentity(implementation="llama-server"),
        model=None, requested_cmd="", final_cmd="", rewrites=(), plan=plan, assessments=(),
        observed={}, differences=(), checks=(), created_at="2026-09-12T00:00:00Z",
    )


# ── physical_budgets ─────────────────────────────────────────────────────────

def test_one_budget_per_physical_gpu_never_summed_by_index_alone():
    snap = _snapshot(GpuInfo(index=0, name="A", uuid="GPU-a"))
    vram = _vram(_card(0, "GPU-a", 16, 5))
    budgets = mb.physical_budgets(snapshot=snap, vram=vram, now=1_000_000.0)
    assert set(budgets) == {"uuid:GPU-a"}
    b = budgets["uuid:GPU-a"]
    assert b.gpu_key == "uuid:GPU-a"
    assert b.total_bytes == 16 * GIB
    assert b.components.free.bytes == 11 * GIB
    assert b.components.free.source == "observed"


def test_t14_two_endpoints_same_physical_gpu_fold_into_one_budget_two_consumers():
    snap = _snapshot(GpuInfo(index=0, name="A", uuid="GPU-shared"))
    vram = _vram(_card(0, "GPU-shared", 24, 18))
    placement = {"qwen3.8:8b": {"gpus": [0], "per_gpu": [{"index": 0, "bytes": 8 * GIB}],
                                "placement": "single", "pid": 111}}
    receipts = [_receipt("serve-aaaaaaaa", gpus_plan=[0], weights_bytes=6 * GIB)]
    budgets = mb.physical_budgets(
        snapshot=snap, vram=vram, placement=placement, residents=[{"name": "qwen3.8:8b"}],
        receipts=receipts, now=1_000_000.0,
    )
    assert len(budgets) == 1
    b = next(iter(budgets.values()))
    kinds = sorted(c.kind for c in b.consumers)
    assert kinds == ["faustus_serve", "ollama"]
    ollama_consumer = next(c for c in b.consumers if c.kind == "ollama")
    assert ollama_consumer.bytes == 8 * GIB
    assert ollama_consumer.source == "observed"
    serve_consumer = next(c for c in b.consumers if c.kind == "faustus_serve")
    assert serve_consumer.bytes == 6 * GIB
    assert serve_consumer.source == "reported_engine"
    # other_processes = used - attributed = 18 - 14 = 4 GiB, all bytes known -> observed
    assert b.components.other_processes.bytes == 4 * GIB
    assert b.components.other_processes.source == "observed"


def test_windows_no_per_process_bytes_marks_other_processes_estimated_with_note():
    snap = _snapshot(GpuInfo(index=0, name="A", uuid="GPU-a"))
    vram = _vram(_card(0, "GPU-a", 16, 10))
    # Windows: nvidia-smi gives no per-process bytes for this consumer.
    placement = {"qwen3.5:9b": {"gpus": [0], "per_gpu": [{"index": 0, "bytes": None}],
                                "placement": "single", "pid": 5}}
    budgets = mb.physical_budgets(
        snapshot=snap, vram=vram, placement=placement, residents=[{"name": "qwen3.5:9b"}],
        now=1_000_000.0,
    )
    b = next(iter(budgets.values()))
    assert b.consumers[0].bytes is None
    assert b.consumers[0].source == "estimated"
    assert b.components.other_processes.source == "estimated"
    assert "Windows reports no per-process bytes" in b.components.other_processes.note


def test_shared_spill_from_wddm_never_counted_as_vram():
    snap = _snapshot(GpuInfo(index=0, name="A", uuid="GPU-a"))
    vram = _vram(_card(0, "GPU-a", 16, 15))
    wddm = {"supported": True, "ollama": {"shared": 2 * GIB}}
    budgets = mb.physical_budgets(snapshot=snap, vram=vram, wddm=wddm, now=1_000_000.0)
    b = next(iter(budgets.values()))
    assert b.shared_spill.bytes == 2 * GIB
    assert b.shared_spill.source == "observed"
    # `free`/`total_bytes` must never have absorbed the shared figure.
    assert b.total_bytes == 16 * GIB
    assert b.components.free.bytes == 1 * GIB


def test_stale_when_observed_at_older_than_max_age_s():
    snap = _snapshot(GpuInfo(index=0, name="A", uuid="GPU-a"), observed_at="2026-09-12T00:00:00Z")
    vram = _vram(_card(0, "GPU-a", 16, 5))
    # observed_at epoch + 60s later, max_age_s=30 -> stale.
    import datetime
    now = datetime.datetime(2026, 9, 12, 0, 1, 0, tzinfo=datetime.timezone.utc).timestamp()
    budgets = mb.physical_budgets(snapshot=snap, vram=vram, max_age_s=30.0, now=now)
    assert next(iter(budgets.values())).stale is True

    now_fresh = datetime.datetime(2026, 9, 12, 0, 0, 10, tzinfo=datetime.timezone.utc).timestamp()
    budgets2 = mb.physical_budgets(snapshot=snap, vram=vram, max_age_s=30.0, now=now_fresh)
    assert next(iter(budgets2.values())).stale is False


def test_no_identity_falls_back_to_index_scoped_key_but_gpu_key_stays_none():
    snap = _snapshot(GpuInfo(index=0, name="mystery"))  # no uuid, no bus_id
    vram = _vram(_card(0, "", 16, 5, name="mystery"))
    budgets = mb.physical_budgets(snapshot=snap, vram=vram, now=1_000_000.0)
    assert set(budgets) == {"index:0"}
    assert budgets["index:0"].gpu_key is None


# ── estimate_candidate ───────────────────────────────────────────────────────

def test_estimate_single_observation_basis_and_totals():
    est = mb.estimate_candidate(
        weights_bytes=8 * GIB, arch=None, ctx=8192, slots=1,
        kv_observations=[{"per_token": 65536.0, "ctx": 8192}],
        engine_implementation="ollama",
    )
    assert est.basis == "single_observation"
    assert est.complete is True
    assert est.weights.bytes == 8 * GIB
    assert est.weights.source == "observed"
    assert est.kv_state.bytes == 65536 * 8192
    assert est.validity.ctx_min == 8192 and est.validity.ctx_max == 8192
    assert est.total_lower == est.total_upper
    assert est.total_lower == 8 * GIB + 65536 * 8192 + mb.vram_fit.DEFAULT_RESERVE_BYTES


def test_estimate_single_observation_outside_ctx_only_widens_upper():
    est = mb.estimate_candidate(
        weights_bytes=8 * GIB, arch=None, ctx=32768,  # far past the observed 8192
        kv_observations=[{"per_token": 65536.0, "ctx": 8192}],
        engine_implementation="ollama",
    )
    assert "extrapolated beyond measured range" in est.kv_state.note
    assert est.complete is False  # an extrapolated KV term is not a complete estimate
    assert est.total_upper is not None
    assert est.total_lower is not None
    assert est.total_lower < est.total_upper  # kv only entered the upper bound


def test_estimate_fitted_basis_reports_domain():
    est = mb.estimate_candidate(
        weights_bytes=8 * GIB, arch=None, ctx=16384,
        kv_observations=[{"per_token": 65536.0, "ctx": 4096}, {"per_token": 49152.0, "ctx": 16384}],
        engine_implementation="ollama",
    )
    assert est.basis == "fitted"
    assert est.validity.ctx_min == 4096
    assert est.validity.ctx_max == 16384
    assert "fitted over" in est.kv_state.note
    assert est.kv_state.bytes is not None
    assert est.complete is True  # ctx=16384 is exactly at the domain edge, not outside it


def test_estimate_fit_overhead_recovers_linear_relationship():
    # total_kv(ctx) = 1_000_000 + 1000*ctx, sampled at two contexts.
    def total(ctx):
        return 1_000_000 + 1000 * ctx
    obs = [{"per_token": total(4096) / 4096, "ctx": 4096}, {"per_token": total(16384) / 16384, "ctx": 16384}]
    fit = mb.fit_overhead(obs)
    assert fit["fixed"] == pytest.approx(1_000_000, rel=1e-6)
    assert fit["per_token"] == pytest.approx(1000, rel=1e-6)
    assert fit["ctx_min"] == 4096 and fit["ctx_max"] == 16384


def test_estimate_hybrid_metadata_basis_preserves_hybrid_note():
    est = mb.estimate_candidate(
        weights_bytes=4 * GIB, arch={"kind": "dense", "kv_per_token": 8192.0, "hybrid": True},
        ctx=8192, kv_observations=[], engine_implementation="ollama",
    )
    assert est.basis == "metadata"
    assert "hybrid" in est.kv_state.note
    assert est.kv_state.bytes == 8192 * 8192
    assert est.complete is True


def test_estimate_unknown_architecture_is_incomplete_not_false_precision():
    est = mb.estimate_candidate(
        weights_bytes=8 * GIB, arch={"kind": "unknown"}, ctx=8192,
        kv_observations=[], engine_implementation="ollama",
    )
    assert est.basis == "incomplete"
    assert est.complete is False
    assert est.kv_state.bytes is None
    assert est.kv_state.source == "absent"
    assert est.total_lower == 8 * GIB + mb.vram_fit.DEFAULT_RESERVE_BYTES
    assert est.total_upper is None


def test_estimate_no_weights_bytes_is_incomplete():
    est = mb.estimate_candidate(weights_bytes=None, arch={"kind": "dense", "kv_per_token": 100.0},
                                ctx=8192, kv_observations=[], engine_implementation="ollama")
    assert est.complete is False
    assert est.total_lower is None
    assert est.weights.source == "absent"


def test_estimate_slots_widen_upper_by_25_percent_of_kv():
    est1 = mb.estimate_candidate(weights_bytes=8 * GIB, arch=None, ctx=8192, slots=1,
                                 kv_observations=[{"per_token": 65536.0, "ctx": 8192}],
                                 engine_implementation="ollama")
    est4 = mb.estimate_candidate(weights_bytes=8 * GIB, arch=None, ctx=8192, slots=4,
                                 kv_observations=[{"per_token": 65536.0, "ctx": 8192}],
                                 engine_implementation="ollama")
    assert "4 slots is an extrapolation" in est4.kv_state.note
    assert est4.kv_state.bytes == est1.kv_state.bytes * 4
    assert est4.validity.slots == 4
    # total_upper = weights + margin + kv*4 + 0.25*(kv*4)
    expected_upper = 8 * GIB + mb.vram_fit.DEFAULT_RESERVE_BYTES + int(est1.kv_state.bytes * 4 * 1.25)
    assert est4.total_upper == expected_upper


def test_estimate_paged_kv_note_for_vllm_and_sglang():
    est = mb.estimate_candidate(weights_bytes=1 * GIB, arch={"kind": "dense", "kv_per_token": 10.0},
                                ctx=1024, engine_implementation="vllm")
    assert any("paged KV" in n for n in est.notes)
    est2 = mb.estimate_candidate(weights_bytes=1 * GIB, arch={"kind": "dense", "kv_per_token": 10.0},
                                 ctx=1024, engine_implementation="ollama")
    assert not any("paged KV" in n for n in est2.notes)


# ── admissible ────────────────────────────────────────────────────────────────

def test_admissible_stale_budget_is_always_unknown():
    snap = _snapshot(GpuInfo(index=0, name="A", uuid="GPU-a"), observed_at="2026-09-12T00:00:00Z")
    vram = _vram(_card(0, "GPU-a", 16, 1))
    import datetime
    now = datetime.datetime(2026, 9, 12, 0, 5, 0, tzinfo=datetime.timezone.utc).timestamp()
    budgets = mb.physical_budgets(snapshot=snap, vram=vram, max_age_s=30.0, now=now)
    budget = next(iter(budgets.values()))
    est = mb.estimate_candidate(weights_bytes=1 * GIB, arch=None,
                                kv_observations=[{"per_token": 1.0, "ctx": 8}], ctx=8,
                                engine_implementation="ollama")
    verdict = mb.admissible(budget, est, now=now)
    assert verdict["verdict"] == "unknown"
    assert "stale" not in verdict["reason"] or "old" in verdict["reason"]
    assert "refresh" in verdict["reason"]


def test_admissible_incomplete_estimate_is_unknown_never_fits():
    snap = _snapshot(GpuInfo(index=0, name="A", uuid="GPU-a"))
    vram = _vram(_card(0, "GPU-a", 64, 0))  # plenty of free room
    budgets = mb.physical_budgets(snapshot=snap, vram=vram, now=1_000_000.0)
    budget = next(iter(budgets.values()))
    est = mb.estimate_candidate(weights_bytes=1 * GIB, arch={"kind": "unknown"}, ctx=8192,
                                kv_observations=[], engine_implementation="ollama")
    verdict = mb.admissible(budget, est)
    assert verdict["verdict"] == "unknown"
    assert "incomplete" in verdict["reason"]


def test_admissible_fits_and_does_not_fit():
    snap = _snapshot(GpuInfo(index=0, name="A", uuid="GPU-a"))
    small_vram = _vram(_card(0, "GPU-a", 4, 3.9))  # ~100 MiB free
    big_vram = _vram(_card(0, "GPU-a", 64, 1))
    est = mb.estimate_candidate(weights_bytes=1 * GIB, arch=None,
                                kv_observations=[{"per_token": 1.0, "ctx": 8}], ctx=8,
                                engine_implementation="ollama")

    budgets_small = mb.physical_budgets(snapshot=snap, vram=small_vram, now=1_000_000.0)
    v_small = mb.admissible(next(iter(budgets_small.values())), est)
    assert v_small["verdict"] == "does_not_fit"
    assert v_small["shortfall_bytes"] > 0

    budgets_big = mb.physical_budgets(snapshot=snap, vram=big_vram, now=1_000_000.0)
    v_big = mb.admissible(next(iter(budgets_big.values())), est)
    assert v_big["verdict"] == "fits"
    assert v_big["shortfall_bytes"] is None


# ── system_memory ─────────────────────────────────────────────────────────────

def test_system_memory_never_raises_and_declares_its_source():
    result = mb.system_memory()
    assert result["source"] in mb.SYSTEM_MEMORY_SOURCES
    if result["source"] == "absent":
        assert result["ram_total"] is None
