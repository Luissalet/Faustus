"""tests/test_creator_resources.py — WP30: physical inventory + the
conservative admission gate (src/creator/resources.py).

GPU/RAM/disk readers are monkeypatched throughout: nothing here talks to a
real nvidia-smi or PDH counter, but every test goes through the real
`resource_admission` module and a real sqlite file under a temp `DATA_DIR`,
because the two claims worth proving are both about that plumbing actually
working end to end.
"""
from __future__ import annotations

import pytest

from src import gpu_shared_memory as gsm
from src import gpu_topology
from src import memory_budget
from src import resource_admission
from src.contracts.inference import GpuInfo, HardwareSnapshot
from src.creator import resources


@pytest.fixture(autouse=True)
def clean_admission(tmp_path, monkeypatch):
    """A private DATA_DIR per test (so resources.db never leaks between
    tests) and a clean resource_admission/ledger state before and after."""
    monkeypatch.setenv("ODYSSEUS_DATA_DIR", str(tmp_path))
    from src import constants
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    resource_admission.reset_all()
    resources.reset_for_tests()
    yield
    resource_admission.reset_all()
    resources.reset_for_tests()


def _settings(monkeypatch, **overrides):
    from src import settings as settings_mod
    base = dict(settings_mod.DEFAULT_SETTINGS)
    base.update(overrides)
    monkeypatch.setattr(settings_mod, "get_setting",
                        lambda key, default=None: base.get(key, default))


# ── inventory ────────────────────────────────────────────────────────────

def test_two_endpoints_on_the_same_physical_gpu_do_not_duplicate_capacity(monkeypatch):
    # The same physical card (uuid GPU-abc), read once via topology (rich
    # identity) and once via vram_snapshot (the totals) — the shape
    # `physical_budgets` is designed to fold into ONE row.
    snap = HardwareSnapshot(
        host="localhost",
        gpus=(GpuInfo(index=0, name="RTX 4070 Ti", uuid="GPU-abc"),),
        topology="known",
    )
    monkeypatch.setattr(gpu_topology, "snapshot", lambda: snap)
    monkeypatch.setattr(gsm, "vram_snapshot", lambda: {
        "supported": True, "gpus": [
            {"index": 0, "name": "RTX 4070 Ti", "uuid": "GPU-abc",
             "total": 12 * 1024**3, "used": 2 * 1024**3},
        ]})
    inv = resources.gpu_inventory()
    assert inv["reading_ok"] is True
    assert len(inv["gpus"]) == 1
    assert inv["gpus"][0]["gpu_key"] == "uuid:GPU-abc"


def test_two_different_cards_are_not_shown_as_one_device(monkeypatch):
    # 12 + 16 + 16 GB on three distinct uuids must stay three rows, never
    # one pooled "44 GB" device.
    snap = HardwareSnapshot(
        host="localhost",
        gpus=(
            GpuInfo(index=0, name="RTX 4070 Ti", uuid="GPU-a"),
            GpuInfo(index=1, name="RTX 5060 Ti", uuid="GPU-b"),
            GpuInfo(index=2, name="RTX 5060 Ti", uuid="GPU-c"),
        ),
        topology="known",
    )
    monkeypatch.setattr(gpu_topology, "snapshot", lambda: snap)
    monkeypatch.setattr(gsm, "vram_snapshot", lambda: {
        "supported": True, "gpus": [
            {"index": 0, "name": "RTX 4070 Ti", "uuid": "GPU-a", "total": 12 * 1024**3, "used": 0},
            {"index": 1, "name": "RTX 5060 Ti", "uuid": "GPU-b", "total": 16 * 1024**3, "used": 0},
            {"index": 2, "name": "RTX 5060 Ti", "uuid": "GPU-c", "total": 16 * 1024**3, "used": 0},
        ]})
    inv = resources.gpu_inventory()
    assert len(inv["gpus"]) == 3
    totals = sorted(g["total_bytes"] for g in inv["gpus"])
    assert totals == [12 * 1024**3, 16 * 1024**3, 16 * 1024**3]


def test_an_unidentified_gpu_is_never_reported_as_free_capacity(monkeypatch):
    monkeypatch.setattr(gpu_topology, "snapshot", lambda: HardwareSnapshot(host="localhost"))
    monkeypatch.setattr(gsm, "vram_snapshot", lambda: {"supported": False, "reason": "no nvidia-smi"})
    inv = resources.gpu_inventory()
    assert inv["gpus"] == []
    assert inv["reading_ok"] is False


def test_device_inventory_reports_ram_disk_cpu(monkeypatch, tmp_path):
    monkeypatch.setattr(gpu_topology, "snapshot", lambda: HardwareSnapshot(host="localhost"))
    monkeypatch.setattr(gsm, "vram_snapshot", lambda: {"supported": False, "reason": "none"})
    monkeypatch.setattr(memory_budget, "system_memory",
                        lambda: {"ram_total": 32 * 1024**3, "ram_available": 20 * 1024**3, "source": "psutil"})
    inv = resources.device_inventory()
    assert inv["ram"]["ram_total"] == 32 * 1024**3
    assert inv["disk"]["path"].endswith("creator")
    assert isinstance(inv["cpu"]["logical_cpus"], int)
    assert inv["mode"] == "serial"


# ── serial mode ──────────────────────────────────────────────────────────

def test_serial_mode_admits_one_and_queues_the_second(monkeypatch):
    _settings(monkeypatch, creator_resource_mode="serial")
    f = resources.Footprint(vram_bytes=1_000_000_000, source="estimated")
    first = resources.admit(f, "http://gpu1:8188", run_id="run1")
    assert first.ok is True
    assert first.lease_id

    second = resources.admit(f, "http://gpu1:8188", run_id="run2")
    assert second.ok is False
    assert second.wait_for == "run1"
    assert "busy" in second.reason

    # A different device is unaffected by the first device's lease.
    third = resources.admit(f, "http://gpu2:8188", run_id="run3")
    assert third.ok is True
    resources.release("run1")
    resources.release("run3")


def test_serial_mode_releases_and_admits_the_next(monkeypatch):
    _settings(monkeypatch, creator_resource_mode="serial")
    f = resources.Footprint(vram_bytes=1, source="estimated")
    a = resources.admit(f, "http://gpu1:8188", run_id="a")
    assert a.ok is True
    blocked = resources.admit(f, "http://gpu1:8188", run_id="b")
    assert blocked.ok is False

    assert resources.release("a") is True
    freed = resources.admit(f, "http://gpu1:8188", run_id="b")
    assert freed.ok is True
    resources.release("b")


def test_release_on_a_run_that_was_never_admitted_is_a_harmless_no_op(monkeypatch):
    assert resources.release("never-admitted") is False


def test_release_after_failure_frees_the_slot_for_the_next_job(monkeypatch):
    """Same shape as a run that failed mid-render: the caller releases in
    its except/finally path regardless of outcome."""
    _settings(monkeypatch, creator_resource_mode="serial")
    f = resources.Footprint(vram_bytes=1, source="estimated")
    a = resources.admit(f, "http://gpu1:8188", run_id="a")
    assert a.ok is True
    # simulate: the job blew up before it ever reached the engine
    resources.release("a")
    b = resources.admit(f, "http://gpu1:8188", run_id="b")
    assert b.ok is True, "a released lease (even after a failure) must free the device"
    resources.release("b")


# ── measured mode ────────────────────────────────────────────────────────

def test_measured_mode_admits_two_that_fit_and_rejects_a_third_that_does_not(monkeypatch):
    _settings(monkeypatch, creator_resource_mode="measured", creator_vram_margin_mb=512)
    device = {"id": "http://gpu1:8188", "free_bytes": 10 * 1024**3}
    small = resources.Footprint(vram_bytes=3 * 1024**3, source="estimated")

    a = resources.admit(small, device, run_id="a")
    assert a.ok is True
    b = resources.admit(small, device, run_id="b")
    assert b.ok is True, "3 + 3 = 6 GiB fits under 10 GiB minus a 512 MiB margin"

    big = resources.Footprint(vram_bytes=5 * 1024**3, source="estimated")
    c = resources.admit(big, device, run_id="c")
    assert c.ok is False
    assert c.wait_for in ("a", "b")
    assert "measured mode" in c.reason

    resources.release("a")
    resources.release("b")


def test_measured_mode_falls_back_to_serial_when_device_capacity_is_unknown(monkeypatch):
    _settings(monkeypatch, creator_resource_mode="measured")
    f = resources.Footprint(vram_bytes=1, source="estimated")
    # a bare string device: no free_bytes/total_bytes to reason about
    a = resources.admit(f, "http://gpu1:8188", run_id="a")
    assert a.ok is True
    b = resources.admit(f, "http://gpu1:8188", run_id="b")
    assert b.ok is False, "an unmeasured device must never be treated as free capacity"
    resources.release("a")


def test_estimate_and_record_measurement_widen_never_narrow(monkeypatch):
    resources.record_measurement("image.product", "1.0.0", "http://gpu1:8188", vram_bytes=2 * 1024**3)
    f = resources.estimate_footprint("image.product", "1.0.0", "http://gpu1:8188")
    assert f.source == "measured"
    assert f.vram_bytes == 2 * 1024**3

    resources.record_measurement("image.product", "1.0.0", "http://gpu1:8188", vram_bytes=1 * 1024**3)
    f2 = resources.estimate_footprint("image.product", "1.0.0", "http://gpu1:8188")
    assert f2.vram_bytes == 2 * 1024**3, "a smaller single sample must not shrink the stored footprint"

    resources.record_measurement("image.product", "1.0.0", "http://gpu1:8188", vram_bytes=3 * 1024**3)
    f3 = resources.estimate_footprint("image.product", "1.0.0", "http://gpu1:8188")
    assert f3.vram_bytes == 3 * 1024**3


def test_a_measurement_at_one_shape_is_not_reused_at_a_different_shape(monkeypatch):
    small_shape = resources.shape_key({"width": 512, "height": 512})
    big_shape = resources.shape_key({"width": 1920, "height": 1080})
    resources.record_measurement("video.clip", "1.0.0", "http://gpu1:8188",
                                  vram_bytes=2 * 1024**3, shape=small_shape)
    unmeasured = resources.estimate_footprint("video.clip", "1.0.0", "http://gpu1:8188", shape=big_shape)
    assert unmeasured.source == "estimated", "512px footprint must not be claimed as proof of 1080p fit"


def test_unmeasured_op_gets_a_flat_conservative_estimate(monkeypatch):
    f = resources.estimate_footprint("never.seen", "9.9.9", "http://gpu9:8188")
    assert f.source == "estimated"
    assert f.vram_bytes > 0


# ── inflight settlement (real before/after delta) ───────────────────────

def test_settle_inflight_records_a_positive_delta_as_a_measurement(monkeypatch):
    resources.note_inflight("run1", "image.product", "1.0.0", "http://gpu1:8188",
                            device="http://gpu1:8188",
                            footprint=resources.Footprint(vram_bytes=1, source="estimated"),
                            vram_before_bytes=2 * 1024**3)
    resources.settle_inflight("run1", vram_after_bytes=5 * 1024**3)
    f = resources.estimate_footprint("image.product", "1.0.0", "http://gpu1:8188")
    assert f.source == "measured"
    assert f.vram_bytes == 3 * 1024**3


def test_settle_inflight_ignores_a_negative_or_missing_delta(monkeypatch):
    resources.note_inflight("run1", "image.product", "1.0.0", "http://gpu1:8188",
                            device="http://gpu1:8188",
                            footprint=resources.Footprint(vram_bytes=1, source="estimated"),
                            vram_before_bytes=5 * 1024**3)
    resources.settle_inflight("run1", vram_after_bytes=4 * 1024**3)  # went DOWN
    f = resources.estimate_footprint("image.product", "1.0.0", "http://gpu1:8188")
    assert f.source == "estimated", "a negative pool-wide delta is noise, not a measurement"

    # settling twice (or a run that was never noted) is a harmless no-op
    resources.settle_inflight("run1", vram_after_bytes=9 * 1024**3)
    resources.settle_inflight("never-noted", vram_after_bytes=9 * 1024**3)
