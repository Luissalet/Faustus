"""PENDIENTES CMP-08 follow-up: `local_latency`'s `generation_tps` stayed
`"unknown"` whenever a model had not yet replied THIS process's lifetime —
"sin GPU medida nunca sale un número" even when Faustus had already measured
that model's decode speed in a previous run. `llm_core.remember_local_speed`
now persists its figures to a small on-disk cache, and `local_speed()` falls
back to it before finally giving up as unknown.

Tests here reset `_LOCAL_SPEED_LOADED_FROM_DISK` locally (the autouse
`isolated_local_speed_cache` fixture in conftest.py pre-sets it to True, so
by default no test touches disk at all) to exercise the hydration path
itself against a private tmp file.
"""

from __future__ import annotations

import json

from src import llm_core


def test_remember_local_speed_persists_to_disk(tmp_path, monkeypatch):
    path = tmp_path / "local_speed.json"
    monkeypatch.setattr(llm_core, "_local_speed_path", lambda: str(path))
    monkeypatch.setattr(llm_core, "_LOCAL_SPEED_LAST_PERSIST", 0.0)

    llm_core.remember_local_speed("qwen3.8:27b-q4_K_M", 200, 10_000_000_000)  # 20 tok/s
    llm_core.flush_local_speed_for_tests()

    assert path.exists()
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["qwen3.8:27b-q4_K_M"]["tps"] == 20.0


def test_local_speed_falls_back_to_disk_when_never_measured_this_run(tmp_path, monkeypatch):
    path = tmp_path / "local_speed.json"
    path.write_text(json.dumps({"qwen3.5:9b": {"tps": 33.5, "samples": 4.0}}), encoding="utf-8")
    monkeypatch.setattr(llm_core, "_local_speed_path", lambda: str(path))
    monkeypatch.setattr(llm_core, "_LOCAL_SPEED_LOADED_FROM_DISK", False)

    # Nothing measured this process yet -> reads the persisted figure.
    assert llm_core.local_speed("qwen3.5:9b") == 33.5
    # A model with no entry anywhere (never measured, ever) stays unknown.
    assert llm_core.local_speed("some-other-model") is None


def test_a_live_measurement_this_run_wins_over_the_stale_disk_figure(tmp_path, monkeypatch):
    path = tmp_path / "local_speed.json"
    path.write_text(json.dumps({"m": {"tps": 5.0, "samples": 1.0}}), encoding="utf-8")
    monkeypatch.setattr(llm_core, "_local_speed_path", lambda: str(path))
    monkeypatch.setattr(llm_core, "_LOCAL_SPEED_LOADED_FROM_DISK", False)
    monkeypatch.setattr(llm_core, "_LOCAL_SPEED_LAST_PERSIST", 0.0)

    llm_core.remember_local_speed("m", 200, 5_000_000_000)  # 40 tok/s, measured live
    assert llm_core.local_speed("m") == 40.0


def test_missing_or_corrupt_disk_cache_is_never_fatal(tmp_path, monkeypatch):
    missing = tmp_path / "does_not_exist.json"
    monkeypatch.setattr(llm_core, "_local_speed_path", lambda: str(missing))
    monkeypatch.setattr(llm_core, "_LOCAL_SPEED_LOADED_FROM_DISK", False)
    assert llm_core.local_speed("m") is None

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(llm_core, "_local_speed_path", lambda: str(corrupt))
    monkeypatch.setattr(llm_core, "_LOCAL_SPEED_LOADED_FROM_DISK", False)
    assert llm_core.local_speed("m") is None


def test_local_latency_for_uses_the_disk_fallback_transparently(tmp_path, monkeypatch):
    from src.workflow_cost_estimate import local_latency_for

    path = tmp_path / "local_speed.json"
    path.write_text(json.dumps({"qwen3.5:9b": {"tps": 12.0, "samples": 2.0}}), encoding="utf-8")
    monkeypatch.setattr(llm_core, "_local_speed_path", lambda: str(path))
    monkeypatch.setattr(llm_core, "_LOCAL_SPEED_LOADED_FROM_DISK", False)

    row = local_latency_for("qwen3.5:9b")
    assert row["generation_tps"] == 12.0

    row_unmeasured = local_latency_for("never-seen-model")
    assert row_unmeasured["generation_tps"] == "unknown"
