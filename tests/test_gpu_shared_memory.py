"""Shared GPU memory detection: the rule that decides when to cry wolf.

The numbers in `test_cuda_baseline_is_not_a_spill` are not invented — they are
what the reference box reports with qwen3.5:9b fully resident and generating at
65 tok/s. If the threshold ever regresses to a plain "any shared memory is
bad", this test fails.
"""
import pytest

from src import gpu_shared_memory as gsm

MB = 1024 * 1024
GB = 1024 * MB


def _fake_rows(monkeypatch, rows, procs):
    monkeypatch.setattr(gsm.sys, "platform", "win32")
    monkeypatch.setattr(gsm, "_rows", lambda: rows)
    monkeypatch.setattr(gsm, "runner_pids", lambda: procs)
    gsm.reset_cache()


def test_off_windows_reports_unsupported(monkeypatch):
    monkeypatch.setattr(gsm.sys, "platform", "linux")
    gsm.reset_cache()
    out = gsm.collect()
    assert out["supported"] is False
    assert "Windows" in out["reason"]


def test_instance_name_parsing():
    m = gsm._INSTANCE_RE.search("pid_2448_luid_0x00000000_0x0000edb2_phys_0")
    assert m and m.group(1) == "2448"
    assert m.group(2) == "0x00000000" and m.group(3) == "0x0000edb2"
    assert gsm._INSTANCE_RE.search("something else") is None


def test_cuda_baseline_is_not_a_spill(monkeypatch):
    # Measured: llama-server.exe holding 8461 MB dedicated and a flat 706 MB
    # shared while generating at full speed. Staging buffers, not paging.
    _fake_rows(monkeypatch,
               [{"pid": 100, "luid": "a", "shared": 706 * MB, "dedicated": 8461 * MB}],
               {100: "llama-server.exe"})
    out = gsm.collect()
    assert out["supported"] is True
    assert out["ollama"]["spilling"] is False
    assert 0.07 < out["ollama"]["shared_fraction"] < 0.08


def test_real_spill_trips_both_tests(monkeypatch):
    _fake_rows(monkeypatch,
               [{"pid": 100, "luid": "a", "shared": 5 * GB, "dedicated": 8 * GB}],
               {100: "llama-server.exe"})
    assert gsm.collect()["ollama"]["spilling"] is True


def test_absolute_floor_and_fraction_both_required(monkeypatch):
    # Over the fraction but tiny in absolute terms: a runner that has barely
    # started is not a spill.
    _fake_rows(monkeypatch,
               [{"pid": 1, "luid": "a", "shared": 300 * MB, "dedicated": 100 * MB}],
               {1: "llama-server.exe"})
    assert gsm.collect()["ollama"]["spilling"] is False
    # Over the absolute floor but a rounding error next to the footprint.
    _fake_rows(monkeypatch,
               [{"pid": 1, "luid": "a", "shared": 2 * GB, "dedicated": 100 * GB}],
               {1: "llama-server.exe"})
    assert gsm.collect()["ollama"]["spilling"] is False


def test_only_runner_processes_are_counted(monkeypatch):
    _fake_rows(monkeypatch,
               [{"pid": 1, "luid": "a", "shared": 9 * GB, "dedicated": 0},
                {"pid": 2, "luid": "a", "shared": 100 * MB, "dedicated": 4 * GB}],
               {2: "llama-server.exe"})
    out = gsm.collect()
    assert out["ollama"]["shared"] == 100 * MB
    assert out["total_shared"] == 9 * GB + 100 * MB
    assert out["ollama"]["spilling"] is False


def test_threshold_from_env(monkeypatch):
    monkeypatch.setenv("FAUSTUS_GPU_SHARED_WARN_BYTES", str(64 * MB))
    assert gsm.warn_threshold_bytes() == 64 * MB
    monkeypatch.setenv("FAUSTUS_GPU_SHARED_WARN_BYTES", "nonsense")
    assert gsm.warn_threshold_bytes() == gsm.DEFAULT_WARN_BYTES


def test_collect_never_raises(monkeypatch):
    monkeypatch.setattr(gsm.sys, "platform", "win32")

    def boom():
        raise OSError("PDH exploded")

    monkeypatch.setattr(gsm, "_rows", boom)
    gsm.reset_cache()
    out = gsm.collect()
    assert out["supported"] is False and "PDH" in out["reason"]


def test_describe_wording(monkeypatch):
    _fake_rows(monkeypatch,
               [{"pid": 1, "luid": "a", "shared": 5 * GB, "dedicated": 8 * GB}],
               {1: "llama-server.exe"})
    assert "paging over PCIe" in gsm.describe(gsm.collect())
    _fake_rows(monkeypatch,
               [{"pid": 1, "luid": "a", "shared": 10 * MB, "dedicated": 8 * GB}],
               {1: "llama-server.exe"})
    assert "no spill" in gsm.describe(gsm.collect())


def test_cache_is_used(monkeypatch):
    calls = []

    monkeypatch.setattr(gsm.sys, "platform", "win32")
    monkeypatch.setattr(gsm, "runner_pids", lambda: {})

    def rows():
        calls.append(1)
        return []

    monkeypatch.setattr(gsm, "_rows", rows)
    gsm.reset_cache()
    gsm.collect()
    gsm.collect()
    assert len(calls) == 1


# ── the counter parsers: one row per (pid, adapter) ─────────────────────────
#
# Instance names as the reference box reports them with qwen3.8:27b-q4_K_M
# split across both cards: the same runner pid under two luids.

_DEDICATED = {
    "pid_15948_luid_0x00000000_0x01b3ff4f_phys_0": 8823 * MB,   # GPU 1's share
    "pid_15948_luid_0x00000000_0x01aec8b1_phys_0": 7400 * MB,   # GPU 0's share
    "pid_2448_luid_0x00000000_0x01aec8b1_phys_0": 300 * MB,     # a browser on GPU 0
}
_SHARED = {
    "pid_15948_luid_0x00000000_0x01b3ff4f_phys_0": 400 * MB,
    "pid_15948_luid_0x00000000_0x01aec8b1_phys_0": 306 * MB,
}
_ADAPTERS = {
    "luid_0x00000000_0x01b3ff4f_phys_0": 9270 * MB,
    "luid_0x00000000_0x01aec8b1_phys_0": 1051 * MB,
}


def test_process_counters_keep_one_row_per_pid_and_luid():
    rows = {(r["pid"], r["luid"]): r for r in gsm.parse_process_counters(_SHARED, _DEDICATED)}
    assert set(rows) == {(15948, "0x00000000_0x01b3ff4f"), (15948, "0x00000000_0x01aec8b1"),
                         (2448, "0x00000000_0x01aec8b1")}
    assert rows[(15948, "0x00000000_0x01b3ff4f")] == {"pid": 15948, "luid": "0x00000000_0x01b3ff4f",
                                                      "shared": 400 * MB, "dedicated": 8823 * MB}
    assert rows[(2448, "0x00000000_0x01aec8b1")]["shared"] == 0     # no shared instance → 0
    assert gsm.parse_process_counters({}, {"garbage": 1}) == []


def test_adapter_counters_are_per_luid_and_ignore_process_instances():
    out = gsm.parse_adapter_counters({**_ADAPTERS, **_DEDICATED})
    assert out == {"0x00000000_0x01b3ff4f": 9270 * MB, "0x00000000_0x01aec8b1": 1051 * MB}


def test_wddm_rows_is_windows_only(monkeypatch):
    monkeypatch.setattr(gsm.sys, "platform", "linux")
    with pytest.raises(OSError):
        gsm.wddm_rows()


def test_wddm_rows_reads_all_three_counter_sets_in_one_query(monkeypatch):
    monkeypatch.setattr(gsm.sys, "platform", "win32")
    asked = []

    def _read(paths):
        asked.append(list(paths))
        return {gsm._SHARED_PATH: _SHARED, gsm._DEDICATED_PATH: _DEDICATED,
                gsm._ADAPTER_DEDICATED_PATH: _ADAPTERS}

    monkeypatch.setattr(gsm, "_read_counters", _read)
    out = gsm.wddm_rows()
    assert len(asked) == 1 and gsm._ADAPTER_DEDICATED_PATH in asked[0]
    assert len(out["processes"]) == 3
    assert out["adapters"]["0x00000000_0x01b3ff4f"] == 9270 * MB


# ── the snapshot: nvidia-smi rows → pool ────────────────────────────────────

_TWO_CARDS = (
    "0, NVIDIA GeForce RTX 4070 Ti, GPU-5ab72dd9-1a45-c3af-5e12-ac7796b1def7, 12282, 1046\n"
    "1, NVIDIA GeForce RTX 5060 Ti, GPU-15d17fee-8c0c-4be3-be46-35fb3e32f2aa, 16311,  441\n"
)


def test_parse_vram_query_reads_index_uuid_and_bytes():
    gpus = gsm.parse_vram_query(_TWO_CARDS)
    assert [g["index"] for g in gpus] == [0, 1]
    assert gpus[0]["uuid"] == "GPU-5ab72dd9-1a45-c3af-5e12-ac7796b1def7"
    assert gpus[0]["name"] == "NVIDIA GeForce RTX 4070 Ti"
    assert gpus[1]["total"] == 16311 * MB and gpus[1]["used"] == 441 * MB
    assert gpus[1]["free"] == (16311 - 441) * MB
    # rows that do not parse or report no memory are skipped, not invented
    assert gsm.parse_vram_query("0, Card, GPU-x, [N/A], [N/A]\n1, Card, GPU-y, 0, 0\n") == []
    assert gsm.parse_vram_query("") == []


def test_pool_name_strips_the_vendor_only_for_a_pool():
    assert gsm.pool_name(["NVIDIA GeForce RTX 4070 Ti"]) == "NVIDIA GeForce RTX 4070 Ti"
    assert gsm.pool_name(["NVIDIA GeForce RTX 4070 Ti", "NVIDIA GeForce RTX 5060 Ti"]) == "RTX 4070 Ti + RTX 5060 Ti"
    assert gsm.pool_name(["NVIDIA RTX A6000", "NVIDIA RTX A6000"]) == "RTX A6000 + RTX A6000"
    assert gsm.pool_name([]) == ""
    assert gsm.short_gpu_name("Tesla T4") == "Tesla T4"


def test_snapshot_from_gpus_sums_the_pool_and_keeps_the_cards():
    snap = gsm.snapshot_from_gpus(gsm.parse_vram_query(_TWO_CARDS))
    assert snap["supported"] is True and snap["count"] == 2
    assert snap["name"] == "RTX 4070 Ti + RTX 5060 Ti"
    assert snap["total"] == (12282 + 16311) * MB
    assert snap["used"] == (1046 + 441) * MB
    assert snap["free"] == snap["total"] - snap["used"]
    assert len(snap["gpus"]) == 2 and snap["gpus"][1]["index"] == 1
    assert gsm.snapshot_from_gpus([])["supported"] is False


def test_one_card_snapshot_is_the_card_plus_count_and_gpus():
    one = gsm.parse_vram_query("0, NVIDIA GeForce RTX 4070 Ti, GPU-5ab72dd9, 12282, 8461\n")
    snap = gsm.snapshot_from_gpus(one)
    assert snap["name"] == "NVIDIA GeForce RTX 4070 Ti"
    assert snap["total"] == 12282 * MB and snap["used"] == 8461 * MB and snap["free"] == 3821 * MB
    assert snap["count"] == 1 and snap["gpus"] == one
