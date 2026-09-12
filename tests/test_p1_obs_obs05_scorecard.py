"""OBS-05 - calidad y rendimiento por perfil (src/scorecard.py).

`aggregate()` already keeps quality (verified_rate), cost (avg_duration_s,
failed_call_rate) and speed (avg_tok_s) as separate fields, never blended —
so the acceptance criterion ("a tok/s win is not shown as a quality win if
errors or time got worse") already held structurally. What was missing:

  * a "linked to model+harness" breakdown finer than "by model alone", so two
    endpoints/hardware are not folded into one row (`aggregate_by`);
  * an explicit regression panel that compares a recent window against the
    baseline and NAMES the exact failure the acceptance criterion warns
    about, gated on having enough samples on both sides (`regression_flags`);
  * a way to reset the local telemetry file (`reset_local_telemetry`).

No invented fixtures: every entry below is built with `sc.build_entry`, the
same function real turns go through, never a hand-rolled dict standing in for
one (rule "sin fixtures inventados").
"""
from __future__ import annotations

import os
import time

import pytest

from src import scorecard as sc


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    monkeypatch.setenv("ODYSSEUS_DATA_DIR", str(d))
    import src.constants as consts
    monkeypatch.setattr(consts, "DATA_DIR", str(d), raising=False)
    return d


def _entry(model, *, endpoint="local", verified=True, duration=10.0, tok_s=None,
          failed_calls=0, tool_calls=1, ts=None):
    e = sc.build_entry(
        session_id="s1", model=model, endpoint_label=endpoint, workspace="/tmp/ws",
        user_text="fix the thing", duration_s=duration, rounds=1,
        harness={"stop_reason": "complete" if verified else "complete_unverified",
                "tool_calls": tool_calls, "failed_calls": failed_calls, "mutations": ["a.py"]},
        tokens_per_second=tok_s,
    )
    if ts is not None:
        e["ts"] = ts
    return e


# ── aggregate_by: linked to model+harness (endpoint) ────────────────────────


def test_aggregate_by_splits_the_same_model_across_endpoints():
    entries = [
        _entry("qwen3.5:9b", endpoint="rtx-4070ti", duration=5.0),
        _entry("qwen3.5:9b", endpoint="rtx-4070ti", duration=5.0),
        _entry("qwen3.5:9b", endpoint="a100-remote", duration=1.0),
        _entry("qwen3.5:9b", endpoint="a100-remote", duration=1.0),
    ]
    rows = sc.aggregate_by(entries, key_fields=("model", "endpoint"))
    by_endpoint = {r["endpoint"]: r for r in rows}
    assert set(by_endpoint) == {"rtx-4070ti", "a100-remote"}
    assert by_endpoint["rtx-4070ti"]["avg_duration_s"] == 5.0
    assert by_endpoint["a100-remote"]["avg_duration_s"] == 1.0
    # aggregate() over the WHOLE set would have blended these into one 3.0s
    # average -- exactly the "different workloads compared as equal" mistake.
    blended = sc.aggregate(entries)
    assert blended[0]["avg_duration_s"] == 3.0
    assert blended[0]["avg_duration_s"] not in (5.0, 1.0)


def test_aggregate_by_default_key_matches_plain_aggregate_when_one_endpoint():
    entries = [_entry("m1"), _entry("m1"), _entry("m2")]
    plain = {r["model"]: r["turns"] for r in sc.aggregate(entries)}
    grouped = {r["model"]: r["turns"] for r in sc.aggregate_by(entries)}
    assert grouped == plain


# ── INF-03: engine / serve_session_id / hardware_profile_id / phases ───────

def test_build_entry_stores_engine_as_a_flat_implementation_string():
    e = sc.build_entry(
        session_id="s1", model="qwen3.5:9b", endpoint_label="local",
        workspace="/tmp/ws", user_text="fix it", duration_s=1.0, rounds=1,
        harness={"stop_reason": "complete", "tool_calls": 1, "failed_calls": 0, "mutations": []},
        engine={"implementation": "ollama", "version": "0.5.1", "host": "127.0.0.1"},
        serve_session_id="serve-aaaaaaaa",
        phases={"total_ms": {"value": 900.0, "source": "observed_client"}},
    )
    assert e["engine"] == "ollama"
    assert e["engine_version"] == "0.5.1"
    assert e["serve_session_id"] == "serve-aaaaaaaa"
    assert e["phases"]["total_ms"]["value"] == 900.0


def test_build_entry_omits_the_new_fields_when_not_given():
    e = sc.build_entry(
        session_id="s1", model="qwen3.5:9b", endpoint_label="local",
        workspace="/tmp/ws", user_text="fix it", duration_s=1.0, rounds=1,
        harness={"stop_reason": "complete", "tool_calls": 1, "failed_calls": 0, "mutations": []},
    )
    assert "engine" not in e
    assert "engine_version" not in e
    assert "serve_session_id" not in e
    assert "hardware_profile_id" not in e
    assert "phases" not in e


def test_old_entries_without_the_new_fields_still_load_and_aggregate(data_dir):
    # An entry written before INF-03 existed -- exactly build_entry's own
    # shape minus the new optional keys, never a hand-rolled dict standing
    # in for one that predates this lote.
    old = _entry("qwen3.5:9b", endpoint="local")
    assert "engine" not in old
    sc.record(old)
    loaded = sc.load()
    assert len(loaded) == 1
    rows = sc.aggregate(loaded)
    assert rows[0]["turns"] == 1


def test_aggregate_by_engine_groups_rows_by_implementation():
    entries = [
        _entry("qwen3.5:9b", duration=2.0),
        _entry("qwen3.5:9b", duration=2.0),
        _entry("qwen3.5:9b", duration=8.0),
    ]
    entries[0]["engine"] = "ollama"
    entries[1]["engine"] = "ollama"
    entries[2]["engine"] = "llama-server"
    rows = sc.aggregate_by(entries, key_fields=("model", "engine"))
    by_engine = {r["engine"]: r for r in rows}
    assert set(by_engine) == {"ollama", "llama-server"}
    assert by_engine["ollama"]["avg_duration_s"] == 2.0
    assert by_engine["llama-server"]["avg_duration_s"] == 8.0


def test_aggregate_by_engine_groups_entries_missing_it_under_unknown():
    entries = [_entry("qwen3.5:9b"), _entry("qwen3.5:9b")]
    entries[0]["engine"] = "ollama"
    # entries[1] has no "engine" at all -- a pre-INF-03 row.
    rows = sc.aggregate_by(entries, key_fields=("model", "engine"))
    by_engine = {r["engine"]: r for r in rows}
    assert set(by_engine) == {"ollama", "?"}


def test_aggregate_by_serve_session_id_splits_restarts_of_the_same_endpoint():
    entries = [
        _entry("qwen3.5:9b", duration=3.0),
        _entry("qwen3.5:9b", duration=9.0),
    ]
    entries[0]["serve_session_id"] = "serve-aaaaaaaa"
    entries[1]["serve_session_id"] = "serve-bbbbbbbb"
    rows = sc.aggregate_by(entries, key_fields=("model", "serve_session_id"))
    by_session = {r["serve_session_id"]: r for r in rows}
    assert by_session["serve-aaaaaaaa"]["avg_duration_s"] == 3.0
    assert by_session["serve-bbbbbbbb"]["avg_duration_s"] == 9.0


# ── regression_flags ─────────────────────────────────────────────────────


def test_regression_flags_names_a_faster_but_worse_model():
    now = time.time()
    old_ts = now - 20 * 86400
    entries = []
    # Baseline (old): slower but reliable.
    for _ in range(5):
        entries.append(_entry("m1", verified=True, duration=20.0, tok_s=10.0, ts=old_ts))
    # Recent: faster tok/s, but quality dropped and it got slower overall.
    for _ in range(5):
        entries.append(_entry("m1", verified=False, duration=30.0, tok_s=25.0, ts=now))
    flags = sc.regression_flags(entries, recent_days=7, min_samples=3)
    assert len(flags) == 1
    f = flags[0]
    assert f["model"] == "m1"
    assert f["tok_s_recent"] > f["tok_s_baseline"]
    assert "verified_rate" in f["worsened"]
    assert "avg_duration_s" in f["worsened"]
    assert f["samples_recent"] == 5 and f["samples_baseline"] == 5


def test_regression_flags_silent_when_tok_s_and_quality_both_improve():
    now = time.time()
    old_ts = now - 20 * 86400
    entries = []
    for _ in range(5):
        entries.append(_entry("m1", verified=False, duration=20.0, tok_s=10.0, ts=old_ts))
    for _ in range(5):
        entries.append(_entry("m1", verified=True, duration=10.0, tok_s=25.0, ts=now))
    assert sc.regression_flags(entries, recent_days=7, min_samples=3) == []


def test_regression_flags_silent_below_min_samples():
    """Real gap this closes: three turns in the last week is not a trend."""
    now = time.time()
    old_ts = now - 20 * 86400
    entries = [_entry("m1", verified=True, duration=20.0, tok_s=10.0, ts=old_ts) for _ in range(10)]
    entries += [_entry("m1", verified=False, duration=40.0, tok_s=30.0, ts=now) for _ in range(2)]
    assert sc.regression_flags(entries, recent_days=7, min_samples=3) == []


# ── reset_local_telemetry ───────────────────────────────────────────────


def test_reset_local_telemetry_removes_the_file_and_load_returns_empty(data_dir, monkeypatch):
    monkeypatch.setattr(sc, "_setting", lambda key, default=None: True if key == "agent_scorecard" else default)
    assert sc.record(_entry("m1")) is True
    assert sc.load()
    assert sc.reset_local_telemetry() is True
    assert sc.load() == []
    assert not os.path.isfile(sc._path())


def test_reset_local_telemetry_is_a_no_op_when_nothing_was_recorded(data_dir):
    assert sc.reset_local_telemetry() is True
