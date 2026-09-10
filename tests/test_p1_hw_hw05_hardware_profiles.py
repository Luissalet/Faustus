"""HW-05 - perfiles de hardware medidos (src/hardware_profiles.py).

A profile never starts a probe of its own: `collect_profile` calls
`services.hwfit.hardware.detect_system` (the SAME cached detector
routes/hwfit_routes.py's /system already exposes -- reused, not
reimplemented) and reads whatever `src.llm_core.remember_local_speed` /
`src.vram_fit.KV_RATES` this process has already learned. Real-hardware
subprocess calls (nvidia-smi, wmic...) are substituted the same way the
existing services/hwfit test suite substitutes them (see e.g.
tests/test_hwfit_cpu_only_fallback.py), by monkeypatching the detector
itself, not by mocking `src.hardware_profiles`'s own logic.

The HTTP surface (routes/hwfit_routes.py) is exercised once end-to-end with
TestClient per COMUN rule 7; the profile logic itself (collect/save/compare)
is exercised directly since none of it is behavior that only exists behind a
route.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from src import hardware_profiles as hwp

FAKE_SYSTEM_A = {
    "has_gpu": True, "gpu_name": "NVIDIA GeForce RTX 4070 Ti", "gpu_vram_gb": 12.0,
    "gpu_count": 1, "total_ram_gb": 64.0, "available_ram_gb": 40.0, "backend": "cuda",
}
FAKE_SYSTEM_B = {
    "has_gpu": True, "gpu_name": "NVIDIA A100", "gpu_vram_gb": 80.0,
    "gpu_count": 1, "total_ram_gb": 256.0, "available_ram_gb": 200.0, "backend": "cuda",
}


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    import src.constants as consts
    monkeypatch.setattr(consts, "DATA_DIR", str(tmp_path), raising=False)
    return tmp_path


@pytest.fixture
def fake_detector(monkeypatch):
    def _detect(host="", ssh_port="", platform="", fresh=False):
        return FAKE_SYSTEM_B if host else FAKE_SYSTEM_A
    monkeypatch.setattr("services.hwfit.hardware.detect_system", _detect)
    return _detect


# ── collect_profile: never fabricates a speed it never measured ────────────


def test_collect_profile_has_no_model_speeds_when_nothing_was_measured(fake_detector, monkeypatch):
    monkeypatch.setattr("src.llm_core._LOCAL_SPEED", {}, raising=False)
    monkeypatch.setattr("src.vram_fit.KV_RATES", {}, raising=False)
    profile = hwp.collect_profile()
    assert profile["system"]["gpu_name"] == "NVIDIA GeForce RTX 4070 Ti"
    assert profile["model_speeds"] == []
    assert profile["kv_rates"] == []
    assert profile["disk"]["free_bytes"] >= 0


def test_collect_profile_surfaces_a_real_measured_speed(fake_detector, monkeypatch):
    monkeypatch.setattr("src.llm_core._LOCAL_SPEED", {"qwen3.5:9b": {"tps": 42.5, "samples": 3.0}}, raising=False)
    profile = hwp.collect_profile()
    assert profile["model_speeds"] == [{"model": "qwen3.5:9b", "tokens_per_second": 42.5, "samples": 3}]


def test_collect_profile_for_a_remote_host_has_no_local_disk_or_speeds(fake_detector):
    """A remote machine's disk/model-speed figures are not this process's to
    report -- fabricating them from local state would misattribute them."""
    profile = hwp.collect_profile(host="gpu-box")
    assert profile["system"]["gpu_name"] == "NVIDIA A100"
    assert profile["disk"] is None
    assert profile["model_speeds"] == []


# ── persistence: save / list / get / delete / export / import ──────────────


def test_save_list_get_delete_round_trip(isolated_store, fake_detector):
    p = hwp.save_profile(hwp.collect_profile(label="bench box"))
    assert hwp.get_profile(p["id"])["label"] == "bench box"
    assert p["id"] in {row["id"] for row in hwp.list_profiles()}
    assert hwp.delete_profile(p["id"]) is True
    assert hwp.get_profile(p["id"]) is None
    assert hwp.delete_profile(p["id"]) is False  # already gone


def test_export_then_import_gets_a_fresh_id_and_does_not_collide(isolated_store, fake_detector):
    p = hwp.save_profile(hwp.collect_profile(label="original"))
    exported = hwp.export_profile(p["id"])
    imported = hwp.import_profile(exported)
    assert imported["id"] != p["id"]
    assert imported["label"] == "original"
    assert {p["id"], imported["id"]}.issubset({row["id"] for row in hwp.list_profiles()})


def test_import_rejects_something_that_is_not_a_profile():
    with pytest.raises(ValueError):
        hwp.import_profile({"not": "a profile"})


# ── compare_profiles: measured-only, never fabricated ───────────────────────


def test_compare_profiles_headline_and_measured_flags(fake_detector, monkeypatch):
    monkeypatch.setattr("src.llm_core._LOCAL_SPEED", {"m1": {"tps": 10.0, "samples": 5.0}}, raising=False)
    a = hwp.collect_profile(label="a-box")
    monkeypatch.setattr("src.llm_core._LOCAL_SPEED", {"m2": {"tps": 90.0, "samples": 5.0}}, raising=False)
    b = hwp.collect_profile(host="gpu-box", label="b-box")
    diff = hwp.compare_profiles(a, b)
    assert diff["a"]["headline"]["gpu_vram_gb"] == 12.0
    assert diff["b"]["headline"]["gpu_vram_gb"] == 80.0
    rows = {r["model"]: r for r in diff["models"]}
    assert rows["m1"]["a_measured"] is True and rows["m1"]["b_measured"] is False
    assert rows["m1"]["b_tokens_per_second"] is None  # never fabricated


# ── HTTP surface (routes/hwfit_routes.py) ───────────────────────────────────


def test_hardware_profile_http_roundtrip(isolated_store, fake_detector):
    from routes.hwfit_routes import setup_hwfit_routes
    app = FastAPI()
    app.include_router(setup_hwfit_routes())
    client = TestClient(app)

    r = client.get("/api/hwfit/hardware-profile", params={"save": True, "label": "ci-box"})
    assert r.status_code == 200
    profile = r.json()
    assert profile["label"] == "ci-box"

    r = client.get("/api/hwfit/hardware-profiles")
    assert r.status_code == 200
    assert profile["id"] in {p["id"] for p in r.json()["profiles"]}

    r = client.get(f"/api/hwfit/hardware-profiles/{profile['id']}")
    assert r.status_code == 200

    r = client.get("/api/hwfit/hardware-profiles/does-not-exist")
    assert r.status_code == 404

    r = client.delete(f"/api/hwfit/hardware-profiles/{profile['id']}")
    assert r.status_code == 200
    assert client.get(f"/api/hwfit/hardware-profiles/{profile['id']}").status_code == 404
