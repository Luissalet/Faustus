"""src/gpu_topology.py — INF-05 §11 A2.

No real `nvidia-smi`: `subprocess.run` is monkeypatched, same pattern
`tests/test_gpu_placement.py`/`tests/test_vram_admission.py` use for their
own collectors. Pure parsing (`parse_topology_query`, `heuristic_transport`,
`reconcile_indices`) is tested with plain fixture strings/dataclasses, no
patching needed at all.
"""
from __future__ import annotations

import subprocess

import pytest

from src import gpu_topology as gt
from src.contracts.inference import GpuInfo, HardwareSnapshot, LinkInfo, TransportInfo, reconcile_indices

GIB = 1024 * 1024


# ── parse_topology_query: pure ──────────────────────────────────────────────

def _row(index, name, uuid, bus, total_mib, used_mib, driver, gen_cur, width_cur, gen_max, width_max):
    return ", ".join(str(x) for x in (
        index, name, uuid, bus, total_mib, used_mib, driver, gen_cur, width_cur, gen_max, width_max,
    ))


def _three_gpu_csv() -> str:
    rows = [
        # Wide link, ordinary name: no heuristic.
        _row(0, "NVIDIA GeForce RTX 4070 Ti", "GPU-aaaa", "0000:01:00.0", 12282, 100,
             "535.129.03", 4, 16, 4, 16),
        # [N/A] pcie fields (Windows/unsupported card): link stays entirely absent.
        _row(1, "NVIDIA GeForce RTX 5060 Ti", "GPU-bbbb", "0000:02:00.0", 16311, 50,
             "535.129.03", "[N/A]", "[N/A]", "[N/A]", "[N/A]"),
        # Narrow observed link (x4) on a card whose NAME says "AORUS" — the
        # heuristic must fire because of the link, never because of the name.
        _row(2, "NVIDIA GeForce RTX 5060 Ti (AORUS enclosure)", "GPU-cccc", "0000:03:00.0",
             16311, 0, "535.129.03", 4, 4, 4, 4),
    ]
    return "\n".join(rows) + "\n"


def test_parse_topology_query_three_gpu_fixture():
    gpus = gt.parse_topology_query(_three_gpu_csv())
    assert [g.index for g in gpus] == [0, 1, 2]

    g0 = gpus[0]
    assert g0.name == "NVIDIA GeForce RTX 4070 Ti"
    assert g0.uuid == "GPU-aaaa"
    assert g0.bus_id == "0000:01:00.0"  # normalized upper-case
    assert g0.vram_bytes == 12282 * GIB
    assert g0.driver == "535.129.03"
    assert g0.link == LinkInfo(gen_current=4, width_current=16, gen_max=4, width_max=16, source="observed")
    assert g0.transport is None  # wide link: heuristic never fires

    g1 = gpus[1]
    assert g1.link is None  # every pcie field was [N/A] -> nothing to report, not 0
    assert g1.transport is None

    g2 = gpus[2]
    assert g2.link == LinkInfo(gen_current=4, width_current=4, gen_max=4, width_max=4, source="observed")
    assert g2.transport is not None
    assert g2.transport.kind == "unknown"
    assert g2.transport.source == "heuristic"
    assert "narrow" in g2.transport.note


def test_bus_id_uppercased_and_na_variants_become_none():
    csv = _row(0, "Some GPU", "gpu-lower", "0000:01:00.0", 8192, 0,
              "[Not Supported]", "", "[N/A]", "16", "16")
    gpus = gt.parse_topology_query(csv)
    assert len(gpus) == 1
    g = gpus[0]
    assert g.bus_id == "0000:01:00.0"
    assert g.driver is None  # "[Not Supported]" -> None, never the literal string
    assert g.link.gen_current is None  # "" -> None
    assert g.link.width_current is None  # "[N/A]" -> None
    assert g.link.gen_max == 16
    assert g.link.width_max == 16


def test_heuristic_never_triggered_by_a_commercial_name_alone():
    # Wide link (x16) on a card whose name screams "AORUS eGPU" — the name
    # is not evidence, and must not produce a heuristic transport.
    csv = _row(0, "AORUS RTX 4090 eGPU", "GPU-wide", "0000:01:00.0", 24564, 0,
              "535.129.03", 4, 16, 4, 16)
    gpus = gt.parse_topology_query(csv)
    assert gpus[0].transport is None


def test_heuristic_transport_requires_both_gen_and_width_observed():
    narrow_gpu = GpuInfo(index=0, name="x", link=LinkInfo(gen_current=None, width_current=4, source="observed"))
    assert gt.heuristic_transport(narrow_gpu) is None  # gen_current missing: no guess
    no_link_gpu = GpuInfo(index=0, name="x", link=None)
    assert gt.heuristic_transport(no_link_gpu) is None
    manual_gpu = GpuInfo(index=0, name="x", link=LinkInfo(gen_current=4, width_current=4, source="manual"))
    assert gt.heuristic_transport(manual_gpu) is None  # only fires on an OBSERVED narrow link


def test_parse_topology_query_ignores_header_and_garbage_lines():
    text = "index, name, uuid, pci.bus_id, memory.total, ...\n" + _row(
        0, "GPU", "GPU-x", "0000:01:00.0", 8192, 0, "535", 4, 16, 4, 16,
    )
    gpus = gt.parse_topology_query(text)
    assert len(gpus) == 1
    assert gpus[0].index == 0


# ── snapshot(): subprocess.run patched, cache reset per test ───────────────

@pytest.fixture(autouse=True)
def _reset_gt_cache():
    gt.reset_cache()
    yield
    gt.reset_cache()


class _FakeCompleted:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


def test_snapshot_without_nvidia_smi_is_unknown_not_empty_known(monkeypatch):
    monkeypatch.setattr(gt.gsm, "_nvidia_smi_path", lambda: None)
    snap = gt.snapshot()
    assert snap.topology == "unknown"
    assert snap.gpus == ()
    assert "not found" in snap.provenance


def test_snapshot_reads_and_caches_nvidia_smi(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _FakeCompleted(0, _three_gpu_csv())

    monkeypatch.setattr(gt.gsm, "_nvidia_smi_path", lambda: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(subprocess, "run", fake_run)
    snap = gt.snapshot()
    assert snap.topology == "known"
    assert len(snap.gpus) == 3
    assert snap.observed_at is not None

    # Cached: a second call within the TTL does not invoke nvidia-smi again.
    gt.snapshot()
    assert len(calls) == 1


def test_snapshot_remote_host_is_unknown_with_explicit_reason(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("must not shell out for a remote host"))
    snap = gt.snapshot(host="gpu-box.lan")
    assert snap.topology == "unknown"
    assert snap.host == "gpu-box.lan"
    assert "not read" in snap.provenance or "not implemented" in snap.provenance


# ── reconcile_indices ────────────────────────────────────────────────────────

def _snap(*gpus) -> HardwareSnapshot:
    return HardwareSnapshot(host="box", gpus=tuple(gpus), topology="known")


def test_reconcile_indices_uuid_moved_missing_new_and_unidentifiable():
    previous = _snap(
        GpuInfo(index=0, name="A", uuid="GPU-a"),
        GpuInfo(index=1, name="B", uuid="GPU-b"),
        GpuInfo(index=2, name="C", bus_id=None, uuid=None),  # no identity at all
    )
    current = _snap(
        GpuInfo(index=1, name="A", uuid="GPU-a"),   # moved: 0 -> 1
        GpuInfo(index=2, name="D", uuid="GPU-d"),   # new
        GpuInfo(index=0, name="C", bus_id=None, uuid=None),  # still unidentifiable
        # "B" (GPU-b) is simply gone: missing
    )
    result = {r.key: r for r in reconcile_indices(previous, current) if r.key is not None}
    assert result["uuid:GPU-a"].state == "moved"
    assert result["uuid:GPU-a"].previous_index == 0
    assert result["uuid:GPU-a"].current_index == 1
    assert result["uuid:GPU-b"].state == "missing"
    assert result["uuid:GPU-d"].state == "new"

    unidentifiable = [r for r in reconcile_indices(previous, current) if r.key is None]
    # One unidentifiable GPU on each side -> two "unidentifiable" rows, never "same".
    assert len(unidentifiable) == 2
    assert all(r.state == "unidentifiable" for r in unidentifiable)


def test_reconcile_indices_same_device_same_index():
    previous = _snap(GpuInfo(index=0, name="A", uuid="GPU-a"))
    current = _snap(GpuInfo(index=0, name="A", uuid="GPU-a"))
    result = reconcile_indices(previous, current)
    assert len(result) == 1
    assert result[0].state == "same"


# ── manual annotations ───────────────────────────────────────────────────────

def test_annotate_transport_persists_and_apply_annotations_overlays(monkeypatch, tmp_path):
    from src import hardware_profiles
    monkeypatch.setattr(hardware_profiles, "_path", lambda: str(tmp_path / "hardware_profiles.json"))

    profile = {"id": "prof-1", "label": "box", "captured_at": 0.0, "system": {}, "topology_annotations": {}}
    hardware_profiles.save_profile(profile)

    updated = gt.annotate_transport("prof-1", "uuid:GPU-a", "thunderbolt", "eGPU box", "luis")
    assert updated["id"] == "prof-1"
    stored = hardware_profiles.get_profile("prof-1")
    assert stored["topology_annotations"]["uuid:GPU-a"]["transport"]["kind"] == "thunderbolt"
    assert stored["topology_annotations"]["uuid:GPU-a"]["transport"]["source"] == "manual"
    assert stored["topology_annotations"]["uuid:GPU-a"]["by"] == "luis"

    snap = _snap(GpuInfo(index=0, name="A", uuid="GPU-a"))
    applied = gt.apply_annotations(snap, stored)
    assert applied.gpus[0].transport.kind == "thunderbolt"
    assert applied.gpus[0].transport.source == "manual"


def test_apply_annotations_never_overrides_an_observed_transport():
    observed = TransportInfo(kind="pcie", source="observed", note="")
    snap = _snap(GpuInfo(index=0, name="A", uuid="GPU-a", transport=observed))
    profile = {"topology_annotations": {"uuid:GPU-a": {
        "transport": {"kind": "thunderbolt", "source": "manual", "note": "", "observed_at": None},
        "by": "someone",
    }}}
    applied = gt.apply_annotations(snap, profile)
    assert applied.gpus[0].transport is observed  # unchanged: observed wins, always


def test_apply_annotations_overrides_a_heuristic_guess():
    heuristic = TransportInfo(kind="unknown", source="heuristic", note="narrow link (x4)...")
    snap = _snap(GpuInfo(index=0, name="A", uuid="GPU-a", transport=heuristic))
    profile = {"topology_annotations": {"uuid:GPU-a": {
        "transport": {"kind": "oculink", "source": "manual", "note": "confirmed oculink dock", "observed_at": None},
        "by": "luis",
    }}}
    applied = gt.apply_annotations(snap, profile)
    assert applied.gpus[0].transport.kind == "oculink"
    assert applied.gpus[0].transport.source == "manual"


def test_annotate_transport_rejects_bad_kind(monkeypatch, tmp_path):
    from src import hardware_profiles
    monkeypatch.setattr(hardware_profiles, "_path", lambda: str(tmp_path / "hardware_profiles.json"))
    from src.contracts.base import ContractError
    with pytest.raises(ContractError):
        gt.annotate_transport("prof-x", "uuid:GPU-a", "usb3", "", "")


def test_heuristic_transport_judges_the_current_width_and_names_the_card_maximum():
    """Seen live (12-09-2026): power management sags the generation at idle
    (4070 Ti at Gen1 x16), never the width, and `width_max` is the card's own
    lane count (a 5060 Ti in an AORUS enclosure reports x4 now, x8 max). The
    current width is the link; the maximum only enriches the note."""
    idle_wide = GpuInfo(index=0, name="x", link=LinkInfo(
        gen_current=1, width_current=16, gen_max=4, width_max=16, source="observed"))
    assert gt.heuristic_transport(idle_wide) is None
    enclosure = GpuInfo(index=1, name="y", link=LinkInfo(
        gen_current=4, width_current=4, gen_max=4, width_max=8, source="observed"))
    hint = gt.heuristic_transport(enclosure)
    assert hint is not None and hint.source == "heuristic"
    assert "x4 now, the card supports x8" in hint.note
