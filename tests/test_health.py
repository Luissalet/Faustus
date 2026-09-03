"""The honest health score (src/health.py) and the block /api/system/usage
now carries.

The two design decisions being pinned here are refusals, not features:

* a machine nothing has been collected from scores near ZERO and says
  `collected: false` — absence of signal is not absence of problem;
* a component with no data source contributes 0, is listed in `missing` and
  carries the "no data source yet" wording instead of a plausible zero.

Plus the contract the endpoint must keep: with `agent_health_score` off the
no-parameter response is exactly the document it has always answered, and with
it on the ONLY difference is the `health` key.
"""
from __future__ import annotations

import asyncio
import json

import pytest

import routes.system_usage_routes as sur
from src import health
from tests.test_system_usage_multigpu import box  # noqa: F401  (the faked collectors)


def _usage():
    sur._cache["ts"] = 0.0
    sur._cache["data"] = None
    return asyncio.run(sur.collect_usage())


def _by_name(doc):
    return {c["name"]: c for c in doc["components"]}


# ── the arithmetic ──────────────────────────────────────────────────────────

def test_a_machine_nothing_was_collected_from_scores_near_zero_not_healthy():
    doc = health.score({})
    assert doc["score"] == 0 and doc["grade"] == "F"
    assert doc["collected"] is False
    assert doc["missing"] == health.names() and doc["reporting"] == 0
    assert "absence of signal is not absence of problem" in doc["why"]
    for c in doc["components"]:
        assert c["state"] == "no_data" and c["value"] is None
        assert "no data source yet" in c["why"]


def test_a_missing_component_contributes_zero_and_is_listed_in_missing():
    full = {name: health.reading("ok", "fine") for name in health.names()}
    assert health.score(full)["score"] == 100 and health.score(full)["grade"] == "A"
    without_disk = {k: v for k, v in full.items() if k != "disk"}
    doc = health.score(without_disk)
    weight = _by_name(doc)["disk"]["weight"]
    assert doc["score"] == 100 - weight            # exactly its weight, nothing else
    assert doc["missing"] == ["disk"] and doc["collected"] is True
    assert _by_name(doc)["disk"]["state"] == "no_data"
    assert "no data source yet" in _by_name(doc)["disk"]["why"]
    assert doc["reporting"] == len(health.names()) - 1


def test_an_explicit_no_data_reading_counts_the_same_as_an_absent_one():
    full = {name: health.reading("ok", "fine") for name in health.names()}
    a = health.score({**full, "gpu": health.reading("no_data", "")})
    b = health.score({k: v for k, v in full.items() if k != "gpu"})
    assert a["score"] == b["score"] and a["missing"] == b["missing"] == ["gpu"]


def test_a_full_set_scores_high_and_a_warn_costs_half_its_weight():
    full = {name: health.reading("ok", "fine") for name in health.names()}
    warned = health.score({**full, "vram": health.reading("warn", "5% free")})
    weight = _by_name(warned)["vram"]["weight"]
    assert warned["score"] == round(100 - weight / 2)
    assert warned["grade"] in ("A", "B") and warned["collected"] is True
    bad = health.score({**full, "vram": health.reading("bad", "spilling")})
    assert bad["score"] == 100 - weight


def test_the_grades_are_the_documented_bands():
    assert [health.grade_for(n) for n in (100, 90, 89, 75, 74, 60, 59, 40, 39, 0)] == \
        ["A", "A", "B", "B", "C", "C", "D", "D", "F", "F"]
    assert health.grade_for("nonsense") == "F"


def test_score_is_total_whatever_it_is_handed():
    for junk in (None, 3, "signals", [1, 2], {"gpu": object()}, {"gpu": "ok"}, {"gpu": True}, {"gpu": False}):
        doc = health.score(junk)
        assert 0 <= doc["score"] <= 100 and doc["grade"] in ("A", "B", "C", "D", "F")
        assert len(doc["components"]) == len(health.names())
    assert health.score({"gpu": "ok"})["components"][1]["state"] == "ok"
    assert health.score({"gpu": False})["components"][1]["state"] == "bad"
    assert health.summary(health.score({})).startswith("0/100 (F)")


def test_a_custom_component_set_is_scored_against_its_own_weights():
    comps = (("a", 70, "A"), ("b", 30, "B"))
    doc = health.score({"a": health.reading("ok", "")}, comps)
    assert doc["score"] == 70 and doc["missing"] == ["b"] and doc["of"] == 2


# ── what the endpoint really has ────────────────────────────────────────────

def test_the_usage_endpoint_carries_the_health_block(box):
    data = _usage()
    doc = data["health"]
    assert doc["schema_version"] == 1 and doc["of"] == len(health.names())
    comps = _by_name(doc)
    assert comps["ollama"]["state"] == "ok" and "reachable" in str(comps["ollama"]["value"])
    assert comps["gpu"]["state"] == "ok" and comps["gpu"]["value"] == "2 card(s)"
    assert comps["host"]["state"] == "ok"
    assert comps["runners"]["state"] == "ok" and comps["runners"]["value"] == "none"
    # nothing invented: no dispatched job ran in this process, so no signal
    assert comps["dispatch"]["state"] == "no_data" and "dispatch" in doc["missing"]


def test_the_only_difference_with_the_setting_off_is_the_health_key(box, monkeypatch):
    with_health = _usage()
    monkeypatch.setattr(health, "enabled", lambda: False)
    without = _usage()
    assert "health" not in without
    a = {k: v for k, v in with_health.items() if k not in ("health", "ts")}
    b = {k: v for k, v in without.items() if k != "ts"}
    assert json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)


def test_an_unreachable_ollama_is_bad_but_an_absent_nvidia_smi_is_only_missing(box, monkeypatch):
    async def _dead(client):
        return {"reachable": False, "base": "http://127.0.0.1:11434", "models": [], "error": "connection refused"}
    monkeypatch.setattr(sur, "_collect_ollama", _dead)
    monkeypatch.setattr(sur, "_collect_gpu", lambda: ([], "nvidia-smi: not found"))
    comps = _by_name(_usage()["health"])
    assert comps["ollama"]["state"] == "bad" and "connection refused" in comps["ollama"]["why"]
    # a box with no NVIDIA card has no GPU data source; it is not a card in trouble
    assert comps["gpu"]["state"] == "no_data"
    assert comps["vram"]["state"] == "no_data" and comps["runners"]["state"] == "no_data"


def test_an_nvidia_smi_that_fails_is_bad_not_missing(box, monkeypatch):
    monkeypatch.setattr(sur, "_collect_gpu", lambda: ([], "nvidia-smi exit 9: driver mismatch"))
    comps = _by_name(_usage()["health"])
    assert comps["gpu"]["state"] == "bad" and "driver mismatch" in comps["gpu"]["why"]


def test_spilling_weights_are_bad_however_much_vram_looks_free(box, monkeypatch):
    monkeypatch.setattr(sur.gpu_shared_memory, "collect",
                        lambda: {"supported": True, "ollama": {"spilling": True, "shared": 1}})
    comps = _by_name(_usage()["health"])
    assert comps["vram"]["state"] == "bad" and "PCIe" in comps["vram"]["why"]
    assert "SPILLING" in str(comps["vram"]["value"])


def test_orphaned_runners_show_up_as_the_signal_they_are(box, monkeypatch):
    from src import gpu_placement
    monkeypatch.setattr(gpu_placement, "orphan_runners",
                        lambda gpus: [{"pid": 1, "gpus": [1], "bytes": 1}, {"pid": 2, "gpus": [0], "bytes": 2},
                                      {"pid": 3, "gpus": [0], "bytes": 3}])
    comps = _by_name(_usage()["health"])
    assert comps["runners"]["state"] == "bad" and comps["runners"]["value"] == "3 orphaned"


def test_a_failed_dispatched_job_in_the_last_hour_is_the_dispatch_signal(box, monkeypatch):
    from src import dispatch
    dispatch.reset_for_tests()
    try:
        job = dispatch.DispatchJob("luis", {"tasks": []}, "/tmp/ws", "", "m", None, "Workers")
        job.status, job.finished = "error", __import__("time").time()
        dispatch._jobs[job.id] = job
        comps = _by_name(_usage()["health"])
        assert comps["dispatch"]["state"] == "bad" and "1 of 1 failed" in comps["dispatch"]["why"]
        job.status = "done"
        comps = _by_name(_usage()["health"])
        assert comps["dispatch"]["state"] == "ok" and "none failed" in comps["dispatch"]["why"]
        job.status = "partial"
        assert _by_name(_usage()["health"])["dispatch"]["state"] == "warn"
    finally:
        dispatch.reset_for_tests()


def test_recent_counts_never_touches_the_disk(monkeypatch):
    from src import dispatch
    dispatch.reset_for_tests()
    monkeypatch.setattr(dispatch, "_load_all", lambda: (_ for _ in ()).throw(AssertionError("read the disk")))
    monkeypatch.setattr(dispatch, "_load", lambda job_id: (_ for _ in ()).throw(AssertionError("read the disk")))
    assert dispatch.recent_counts(3600.0) == {"jobs": 0, "done": 0, "partial": 0, "failed": 0,
                                              "cancelled": 0, "live": 0}
    job = dispatch.DispatchJob("luis", {"tasks": []}, "/tmp/ws", "", "m", None, "Workers")
    job.status, job.finished = "done", 10.0
    dispatch._jobs[job.id] = job
    assert dispatch.recent_counts(3600.0, now=20.0)["done"] == 1
    assert dispatch.recent_counts(3600.0, now=1e9)["jobs"] == 0        # older than the window
    dispatch.reset_for_tests()


def test_a_health_block_that_cannot_be_built_never_breaks_the_gauges(box, monkeypatch):
    monkeypatch.setattr(sur, "health_signals", lambda data: (_ for _ in ()).throw(RuntimeError("boom")))
    data = _usage()
    assert "health" not in data and data["gpu_pool"]["count"] == 2


@pytest.mark.parametrize("payload", [{}, {"ollama": None, "gpu": None}, {"errors": "not a list"}])
def test_health_signals_survive_a_payload_that_collected_nothing(payload):
    signals = sur.health_signals(payload)
    doc = health.score(signals)
    assert doc["score"] <= 20 and doc["of"] == len(health.names())
