"""routes/hardware_routes.py — INF-05 §11/§14 A5.

Harness follows `tests/test_inf02_serve_routes.py`'s `_inference_client`:
a bare FastAPI app carrying only this router, `require_admin` patched out,
and every collector this router calls monkeypatched — no real `nvidia-smi`,
no network, no model load.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.hardware_routes as hardware_routes
from src import gpu_shared_memory, gpu_topology, hardware_profiles, launch_receipts
from src import memory_budget, model_architecture, vram_admission
from src.contracts.inference import EngineIdentity, GpuInfo, HardwareSnapshot, LaunchReceipt

GIB = 1024 * 1024 * 1024


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(hardware_routes.setup_hardware_routes())
    return TestClient(app)


def _snap(*gpus, host="box", observed_at="2026-09-12T00:00:00Z") -> HardwareSnapshot:
    return HardwareSnapshot(host=host, gpus=tuple(gpus), topology="known", observed_at=observed_at)


def _vram(*gpus) -> dict:
    return {"supported": True, "name": "pool", "count": len(gpus),
           "total": sum(g["total"] for g in gpus), "used": sum(g["used"] for g in gpus),
           "gpus": [dict(g) for g in gpus]}


def _card(index, uuid, total_gb, used_gb, name="RTX"):
    return {"index": index, "name": name, "uuid": uuid,
           "total": int(total_gb * GIB), "used": int(used_gb * GIB),
           "free": int((total_gb - used_gb) * GIB)}


def _patch_common(monkeypatch, *, snap=None, vram=None):
    monkeypatch.setattr(hardware_routes, "require_admin", lambda request: None)
    monkeypatch.setattr(hardware_routes.gpu_topology, "snapshot",
                        lambda **kw: snap or _snap(GpuInfo(index=0, name="A", uuid="GPU-a")))
    monkeypatch.setattr(hardware_routes.gpu_shared_memory, "vram_snapshot",
                        lambda: vram or _vram(_card(0, "GPU-a", 16, 4)))
    monkeypatch.setattr(hardware_routes.gpu_shared_memory, "collect",
                        lambda: {"supported": False, "reason": "not windows"})
    monkeypatch.setattr(hardware_routes.launch_receipts, "live_receipts", lambda: [])
    monkeypatch.setattr(hardware_routes.hardware_profiles, "list_profiles", lambda: [])


# ── GET /api/hardware/topology ──────────────────────────────────────────────

def test_topology_endpoint_no_saved_profile_has_no_reconciliation(monkeypatch):
    _patch_common(monkeypatch)
    resp = _client().get("/api/hardware/topology")
    assert resp.status_code == 200
    body = resp.json()
    assert body["profile_id"] is None
    assert body["reconciliation"] == []
    assert body["snapshot"]["gpus"][0]["uuid"] == "GPU-a"


def test_topology_endpoint_reconciles_against_most_recent_profile(monkeypatch):
    current = _snap(GpuInfo(index=1, name="A", uuid="GPU-a"))  # was index 0, now 1
    _patch_common(monkeypatch, snap=current)
    previous_snap = _snap(GpuInfo(index=0, name="A", uuid="GPU-a"))
    profile = {"id": "prof-1", "topology": previous_snap.to_dict(), "topology_annotations": {}}
    monkeypatch.setattr(hardware_routes.hardware_profiles, "list_profiles", lambda: [profile])
    resp = _client().get("/api/hardware/topology")
    body = resp.json()
    assert body["profile_id"] == "prof-1"
    assert body["reconciliation"] == [
        {"key": "uuid:GPU-a", "previous_index": 0, "current_index": 1, "state": "moved"},
    ]


def test_topology_endpoint_no_nvidia_smi_reports_unknown_not_500(monkeypatch):
    monkeypatch.setattr(hardware_routes, "require_admin", lambda request: None)
    monkeypatch.setattr(hardware_routes.gpu_topology, "snapshot",
                        lambda **kw: HardwareSnapshot(host="box", gpus=(), topology="unknown",
                                                      provenance="nvidia-smi: not found"))
    monkeypatch.setattr(hardware_routes.hardware_profiles, "list_profiles", lambda: [])
    resp = _client().get("/api/hardware/topology")
    assert resp.status_code == 200
    assert resp.json()["snapshot"]["topology"] == "unknown"


# ── POST /api/hardware/topology/annotate ────────────────────────────────────

def test_annotate_endpoint_persists_profile(monkeypatch):
    monkeypatch.setattr(hardware_routes, "require_admin", lambda request: None)
    captured = {}

    def fake_annotate(profile_id, gpu_key, kind, note, by):
        captured.update(profile_id=profile_id, gpu_key=gpu_key, kind=kind, note=note, by=by)
        return {"id": profile_id or "prof-new", "topology_annotations": {gpu_key: {"kind": kind}}}

    monkeypatch.setattr(hardware_routes.gpu_topology, "annotate_transport", fake_annotate)
    resp = _client().post("/api/hardware/topology/annotate",
                          json={"gpu_key": "uuid:GPU-a", "kind": "thunderbolt", "note": "eGPU"})
    assert resp.status_code == 200
    assert resp.json()["profile"]["id"] == "prof-new"
    assert captured["kind"] == "thunderbolt"


def test_annotate_endpoint_rejects_unknown_kind(monkeypatch):
    monkeypatch.setattr(hardware_routes, "require_admin", lambda request: None)
    resp = _client().post("/api/hardware/topology/annotate",
                          json={"gpu_key": "uuid:GPU-a", "kind": "usb3", "note": ""})
    assert resp.status_code == 400
    assert resp.json()["error_class"] == "hardware.invalid_annotation"


# ── GET /api/hardware/budget ─────────────────────────────────────────────────

def test_budget_endpoint_no_model_no_estimate(monkeypatch):
    _patch_common(monkeypatch)
    resp = _client().get("/api/hardware/budget")
    assert resp.status_code == 200
    body = resp.json()
    assert "uuid:GPU-a" in body["budgets"]
    assert body["estimate"] is None
    assert body["verdict"]["verdict"] == "unknown"
    assert body["system_memory"]["source"] in memory_budget.SYSTEM_MEMORY_SOURCES


def test_budget_endpoint_with_weights_bytes_builds_estimate_and_verdict(monkeypatch):
    _patch_common(monkeypatch, vram=_vram(_card(0, "GPU-a", 64, 0)))
    resp = _client().get("/api/hardware/budget", params={"weights_bytes": 1 * GIB})
    body = resp.json()
    assert body["estimate"] is not None
    assert body["estimate"]["weights"]["bytes"] == 1 * GIB
    # No architecture/KV info given: incomplete, so admissible() must say unknown, never "fits".
    assert body["verdict"]["verdict"] == "unknown"


def test_budget_endpoint_attributes_ollama_residents_via_endpoint(monkeypatch):
    _patch_common(monkeypatch, vram=_vram(_card(0, "GPU-a", 16, 8)))
    monkeypatch.setattr(vram_admission, "ollama_root", lambda url: "http://127.0.0.1:11434")

    def fake_get(root, path, timeout):
        if path == "/api/ps":
            return {"models": [{"name": "qwen3.5:9b", "size_vram": 8 * GIB, "digest": "d1"}]}
        return {"models": [{"name": "qwen3.5:9b", "digest": "d1", "size": 9 * GIB}]}

    monkeypatch.setattr(vram_admission, "_get", fake_get)
    monkeypatch.setattr(hardware_routes.gpu_placement, "placement",
                        lambda root, residents, gpus: {
                            "qwen3.5:9b": {"gpus": [0], "per_gpu": [{"index": 0, "bytes": 8 * GIB}],
                                          "placement": "single", "pid": 42},
                        })
    resp = _client().get("/api/hardware/budget", params={"endpoint": "http://127.0.0.1:11434/v1"})
    body = resp.json()
    budget = body["budgets"]["uuid:GPU-a"]
    assert budget["consumers"][0]["kind"] == "ollama"
    assert budget["consumers"][0]["bytes"] == 8 * GIB


def test_budget_endpoint_never_counts_stale_reading_as_fits(monkeypatch):
    old_snap = _snap(GpuInfo(index=0, name="A", uuid="GPU-a"), observed_at="2020-01-01T00:00:00Z")
    _patch_common(monkeypatch, snap=old_snap, vram=_vram(_card(0, "GPU-a", 64, 0)))
    resp = _client().get("/api/hardware/budget", params={"weights_bytes": 1024})
    assert resp.json()["budgets"]["uuid:GPU-a"]["stale"] is True
    assert resp.json()["verdict"]["verdict"] == "unknown"


# ── GET /api/hardware/context-limits ────────────────────────────────────────

def test_context_limits_native_from_architecture(monkeypatch):
    monkeypatch.setattr(hardware_routes, "require_admin", lambda request: None)
    monkeypatch.setattr(model_architecture, "get_model_architecture",
                        lambda repo, **kw: {"native_context": 8192, "source": "hf_config",
                                            "context_note": "extended by rope_scaling (yarn) to 32768: not a quality guarantee"})
    monkeypatch.setattr(launch_receipts, "find_by_endpoint", lambda host, port: None)
    resp = _client().get("/api/hardware/context-limits", params={"model": "org/model"})
    limits = resp.json()["limits"]
    assert limits["native"]["value"] == 8192
    assert limits["native"]["source"] == "hf_config"
    assert "rope_scaling" in limits["native"]["note"]
    assert limits["configured"]["source"] == "absent"
    assert limits["evaluated"]["source"] == "absent"


def test_context_limits_configured_from_receipt_observed(monkeypatch):
    monkeypatch.setattr(hardware_routes, "require_admin", lambda request: None)
    monkeypatch.setattr(model_architecture, "get_model_architecture",
                        lambda repo, **kw: {"native_context": None, "source": "none", "context_note": None})
    receipt = LaunchReceipt(
        session_id="serve-aaaaaaaa", engine=EngineIdentity(implementation="llama-server", host="127.0.0.1", port=8080),
        model=None, requested_cmd="", final_cmd="", rewrites=(), plan={}, assessments=(),
        observed={"ctx": 16384}, differences=(), checks=(), created_at="2026-09-12T00:00:00Z",
    )
    monkeypatch.setattr(launch_receipts, "find_by_endpoint", lambda host, port: receipt)
    resp = _client().get("/api/hardware/context-limits",
                         params={"model": "x", "endpoint": "http://127.0.0.1:8080"})
    limits = resp.json()["limits"]
    assert limits["configured"]["value"] == 16384
    assert limits["configured"]["source"] == "receipt"


def test_context_limits_evaluated_from_bench_runs(monkeypatch):
    monkeypatch.setattr(hardware_routes, "require_admin", lambda request: None)
    monkeypatch.setattr(model_architecture, "get_model_architecture",
                        lambda repo, **kw: {"native_context": None, "source": "none", "context_note": None})
    monkeypatch.setattr(launch_receipts, "find_by_endpoint", lambda host, port: None)

    from src.contracts.inference import (
        BenchmarkRun, ExecutionMetrics, InferenceProfile, ModelDescriptor, Phases,
        RunBudget, RunConditions, RunSample, RunSummary, SampleQuality, Tokens, MetricValue,
    )
    profile = InferenceProfile(
        id="p1", label="p1", model=ModelDescriptor(artifact_id="org/model"),
        engine=EngineIdentity(implementation="ollama"), hardware_id=None, options={},
        objective="interactive", evaluation="evaluated", fingerprint="abc123", created_at="2026-09-12T00:00:00Z",
        source="manual",
    )
    metrics = ExecutionMetrics(
        phases=Phases(), tokens=Tokens(prompt=MetricValue(value=4096, source="reported_engine")),
        scope="request",
    )
    run = BenchmarkRun(
        id="run-1", suite_id="s", suite_version="1.0.0", profile=profile, baseline_run_id=None,
        state="completed", budget=RunBudget(), conditions=RunConditions(),
        samples=(RunSample(case_id="c1", repeat=1, metrics=metrics, quality=SampleQuality(passed=True)),),
        summary=RunSummary(), interruptions=(), started_at=None, finished_at=None,
    )
    monkeypatch.setattr("src.bench.runner.list_runs", lambda limit=200: [run])
    resp = _client().get("/api/hardware/context-limits", params={"model": "org/model"})
    limits = resp.json()["limits"]
    assert limits["evaluated"]["min"] == 4096
    assert limits["evaluated"]["max"] == 4096
    assert limits["evaluated"]["source"] == "bench_runs"


def test_budget_without_endpoint_attributes_every_local_ollama(monkeypatch):
    """Servers › Physical GPUs asks with no endpoint: the residents of every
    same-machine Ollama declared must still be attributed (seen live: the
    resident 27B showed up as 'other processes (observed)')."""
    import routes.hardware_routes as hr
    import routes.local_models_routes as lmr

    monkeypatch.setattr(lmr, "list_ollama_endpoints", lambda *a, **k: [
        {"root": "http://127.0.0.1:11434", "same_machine": True},
        {"root": "http://10.0.0.9:11434", "same_machine": False},
    ])
    seen = []

    def fake_get(root, path, timeout):
        seen.append((root, path))
        return {"models": [{"name": "qwen:27b", "size_vram": 10}]}

    monkeypatch.setattr(hr.vram_admission, "_get", fake_get)
    monkeypatch.setattr(hr.gpu_shared_memory, "vram_snapshot", lambda: {"supported": False, "reason": "test"})
    monkeypatch.setattr(hr.gpu_placement, "placement", lambda root, residents, gpus: {"qwen:27b": {"per_gpu": [{"index": 0, "bytes": None}], "pid": None}})
    residents, placement, root = hr._residents_and_placement("")
    assert [r["name"] for r in residents] == ["qwen:27b"]
    assert "qwen:27b" in placement
    assert root == "http://127.0.0.1:11434"
    assert all(r == "http://127.0.0.1:11434" for r, _ in seen)  # the remote one is never probed
