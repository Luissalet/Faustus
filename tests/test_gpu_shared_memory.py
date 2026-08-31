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
