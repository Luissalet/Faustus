"""/api/system/usage with two cards (routes/system_usage_routes.py).

The nvidia-smi rows are the reference box as captured today — an RTX 4070 Ti
(12 GB) and an RTX 5060 Ti (16 GB) — in the route's own column order. The
placement pass is faked at the module boundary (src/gpu_placement.py has its
own tests); what is checked here is the shape the widget consumes: per-card
`uuid/bus_id/mem_free/models/runner_pids`, the `gpu_pool` sums, the
`gpus/placement/per_gpu` on every ollama row, and the Fit-to-VRAM advisor
budgeting against the pool with a CUDA context per card.
"""
from __future__ import annotations

import asyncio

import pytest

import routes.system_usage_routes as sur
from src import gpu_placement, gpu_shared_memory, vram_fit

MIB = 1024 ** 2
UUID0 = "GPU-5ab72dd9-1a45-c3af-5e12-ac7796b1def7"
UUID1 = "GPU-15d17fee-8c0c-4be3-be46-35fb3e32f2aa"

# index, name, utilization.gpu, memory.used, memory.total, temperature.gpu,
# power.draw, power.limit, uuid, pci.bus_id, memory.free
TWO_CARDS = (
    f"0, NVIDIA GeForce RTX 4070 Ti, 22, 1046, 12282, 39, 16.50, 285.00, {UUID0}, 00000000:01:00.0, 11236\n"
    f"1, NVIDIA GeForce RTX 5060 Ti, 0, 9263, 16311, 43, 7.20, 180.00, {UUID1}, 00000000:10:00.0, 7048\n"
)
ONE_CARD = f"0, NVIDIA GeForce RTX 4070 Ti, 22, 8461, 12282, 39, 16.50, 285.00, {UUID0}, 00000000:01:00.0, 3821\n"

PS = [
    {"name": "qwen3.5:9b", "model": "qwen3.5:9b", "size": 8_100_000_000, "size_vram": 8_100_000_000,
     "context_length": 32768, "expires_at": "2099-01-01T00:00:00Z",
     "details": {"parameter_size": "9B", "quantization_level": "Q4_K_M", "family": "qwen3"}},
    {"name": "tiny:cpu", "model": "tiny:cpu", "size": 1_000_000_000, "size_vram": 0,
     "context_length": 4096, "details": {}},
]

REPORT = {
    "models": {
        "qwen3.5:9b": {"gpus": [1], "per_gpu": [{"index": 1, "bytes": 8823 * MIB}], "placement": "single", "pid": 15948},
        "tiny:cpu": {"gpus": [], "per_gpu": [], "placement": "cpu", "pid": 16001},
    },
    "gpus": {
        0: {"models": [], "runner_pids": []},
        1: {"models": [{"name": "qwen3.5:9b", "bytes": 8823 * MIB}], "runner_pids": [15948]},
    },
}


@pytest.fixture
def box(monkeypatch):
    state = {"gpu": TWO_CARDS, "ps": PS, "report": REPORT, "report_calls": []}

    async def _ollama(client):
        out = {"reachable": True, "base": "http://127.0.0.1:11434", "models": []}
        for m in state["ps"]:
            size = int(m.get("size") or 0)
            vram = int(m.get("size_vram") or 0)
            pct = round(100.0 * vram / size) if size else 0
            out["models"].append({"name": m["name"], "size": size, "size_vram": vram, "gpu_pct": pct,
                                  "cpu_pct": max(0, 100 - pct), "context_length": m.get("context_length"),
                                  "expires_at": m.get("expires_at"), "parameter_size": None,
                                  "quantization": None, "family": None})
        return out

    def _report(base, models, gpus):
        state["report_calls"].append((base, [m["name"] for m in models], [g["index"] for g in gpus]))
        return state["report"]

    monkeypatch.setattr(sur, "_collect_ollama", _ollama)
    monkeypatch.setattr(sur, "_collect_gpu", lambda: (sur.parse_gpu_query(state["gpu"]), None))
    monkeypatch.setattr(sur, "_collect_host", lambda: {"cpu": {"percent": 1.0, "count": 8}, "ram": {"used": 1, "total": 2, "percent": 50.0}})
    monkeypatch.setattr(sur.gpu_shared_memory, "collect", lambda: {"supported": False, "reason": "test"})
    monkeypatch.setattr(sur, "_collect_policy", lambda: {"exposed": False})
    monkeypatch.setattr(gpu_placement, "report", _report)
    sur._cache["ts"] = 0.0
    sur._cache["data"] = None
    yield state
    sur._cache["ts"] = 0.0
    sur._cache["data"] = None


def _usage():
    return asyncio.run(sur.collect_usage())


# ── parsing ─────────────────────────────────────────────────────────────────

def test_the_query_asks_for_uuid_bus_id_and_free_memory_after_the_old_columns():
    assert sur._NVSMI_FIELDS[:8] == ["index", "name", "utilization.gpu", "memory.used", "memory.total",
                                     "temperature.gpu", "power.draw", "power.limit"]
    assert sur._NVSMI_FIELDS[8:] == ["uuid", "pci.bus_id", "memory.free"]


def test_parse_gpu_query_two_cards():
    gpus = sur.parse_gpu_query(TWO_CARDS)
    assert [g["index"] for g in gpus] == [0, 1]
    g0, g1 = gpus
    assert g0["name"] == "NVIDIA GeForce RTX 4070 Ti" and g0["util"] == 22.0
    assert g0["mem_used"] == 1046.0 and g0["mem_total"] == 12282.0 and g0["mem_free"] == 11236.0
    assert g0["temp"] == 39.0 and g0["power"] == 16.5 and g0["power_limit"] == 285.0
    assert g0["uuid"] == UUID0 and g0["bus_id"] == "00000000:01:00.0"
    assert g1["uuid"] == UUID1 and g1["bus_id"] == "00000000:10:00.0" and g1["mem_total"] == 16311.0
    assert g0["models"] == [] and g0["runner_pids"] == []
    # a row from the old 8-column query is not a card
    assert sur.parse_gpu_query("0, NVIDIA GeForce RTX 4070 Ti, 22, 1046, 12282, 39, 16.50, 285.00\n") == []


def test_parse_gpu_query_tolerates_na_free_memory():
    row = f"0, Card, 5, 100, 1000, 30, 10, 100, {UUID0}, 00000000:01:00.0, [N/A]\n"
    g = sur.parse_gpu_query(row)[0]
    assert g["mem_free"] == 900.0          # derived rather than left empty


# ── the pool ────────────────────────────────────────────────────────────────

def test_gpu_pool_sums_memory_and_power_and_takes_the_worst_util_and_temp():
    pool = sur.gpu_pool(sur.parse_gpu_query(TWO_CARDS))
    assert pool["count"] == 2
    assert pool["mem_used"] == 1046 + 9263 and pool["mem_total"] == 12282 + 16311
    assert pool["mem_free"] == 11236 + 7048
    assert pool["util"] == 22.0 and pool["util_avg"] == 11.0
    assert pool["power"] == 16.5 + 7.2 and pool["power_limit"] == 285.0 + 180.0
    assert pool["temp"] == 43.0
    assert pool["names"] == ["NVIDIA GeForce RTX 4070 Ti", "NVIDIA GeForce RTX 5060 Ti"]
    assert pool["name"] == "RTX 4070 Ti + RTX 5060 Ti"


def test_gpu_pool_is_empty_without_a_card():
    assert sur.gpu_pool([]) == {}


def test_gpu_pool_of_one_card_is_that_card():
    pool = sur.gpu_pool(sur.parse_gpu_query(ONE_CARD))
    assert pool["count"] == 1 and pool["mem_total"] == 12282 and pool["util"] == 22.0
    assert pool["name"] == "NVIDIA GeForce RTX 4070 Ti"


# ── the payload ─────────────────────────────────────────────────────────────

def test_usage_carries_the_pool_the_cards_models_and_each_models_placement(box):
    data = _usage()
    assert data["gpu_pool"]["count"] == 2 and data["gpu_pool"]["mem_total"] == 12282 + 16311
    g0, g1 = data["gpu"]
    # everything the widget read before is still there
    for key in ("index", "name", "util", "mem_used", "mem_total", "temp", "power", "power_limit"):
        assert key in g0, key
    assert g1["uuid"] == UUID1 and g1["mem_free"] == 7048.0
    assert g1["models"] == [{"name": "qwen3.5:9b", "bytes": 8823 * MIB}] and g1["runner_pids"] == [15948]
    assert g0["models"] == [] and g0["runner_pids"] == []
    by_name = {m["name"]: m for m in data["ollama"]["models"]}
    assert by_name["qwen3.5:9b"]["gpus"] == [1] and by_name["qwen3.5:9b"]["placement"] == "single"
    assert by_name["qwen3.5:9b"]["per_gpu"] == [{"index": 1, "bytes": 8823 * MIB}]
    assert by_name["tiny:cpu"]["placement"] == "cpu" and by_name["tiny:cpu"]["gpus"] == []
    # the old keys on the ollama rows are untouched
    assert by_name["qwen3.5:9b"]["gpu_pct"] == 100 and by_name["tiny:cpu"]["cpu_pct"] == 100
    # placement was asked once, with the ps rows and the card list
    assert box["report_calls"] == [("http://127.0.0.1:11434", ["qwen3.5:9b", "tiny:cpu"], [0, 1])]


def test_placement_is_not_asked_with_nothing_loaded_or_no_card(box):
    box["ps"] = []
    data = _usage()
    assert box["report_calls"] == []
    assert data["ollama"]["models"] == [] and data["gpu"][0]["models"] == []
    sur._cache["ts"] = 0.0
    box["ps"] = PS
    box["gpu"] = ""
    data = _usage()
    assert box["report_calls"] == []
    assert data["gpu_pool"] == {}
    # rows without a placement answer still carry the keys, honestly
    by_name = {m["name"]: m for m in data["ollama"]["models"]}
    assert by_name["qwen3.5:9b"] ["placement"] == "unknown" and by_name["qwen3.5:9b"]["gpus"] == []
    assert by_name["tiny:cpu"]["placement"] == "cpu"


def test_one_card_is_exactly_the_old_payload_plus_the_new_keys(box):
    box["gpu"] = ONE_CARD
    box["report"] = {"models": {"qwen3.5:9b": {"gpus": [0], "per_gpu": [{"index": 0, "bytes": 8_100_000_000}],
                                               "placement": "single", "pid": 1},
                                "tiny:cpu": {"gpus": [], "per_gpu": [], "placement": "cpu", "pid": None}},
                     "gpus": {0: {"models": [{"name": "qwen3.5:9b", "bytes": 8_100_000_000}], "runner_pids": [1]}}}
    data = _usage()
    assert len(data["gpu"]) == 1 and data["gpu_pool"]["count"] == 1
    assert data["gpu"][0]["mem_used"] == 8461.0
    assert data["ollama"]["models"][0]["gpus"] == [0]


def test_usage_is_cached_for_a_second(box):
    _usage()
    _usage()
    assert len(box["report_calls"]) == 1


# ── Fit to VRAM against the pool ────────────────────────────────────────────

def _fit(monkeypatch, model="qwen3.8:27b-q4_K_M", file_size=17_000_000_000, target_ctx=None):
    async def _show(client, m):
        return {"model_info": {"general.architecture": "qwen3", "qwen3.block_count": 64,
                               "qwen3.context_length": 131072, "qwen3.attention.head_count": 32,
                               "qwen3.attention.head_count_kv": 8, "qwen3.attention.key_length": 128,
                               "qwen3.attention.value_length": 128}}

    async def _size(client, m):
        return file_size

    monkeypatch.setattr(sur, "_model_show", _show)
    monkeypatch.setattr(sur, "_file_size", _size)
    return asyncio.run(sur.collect_fit(model, target_ctx))


def test_fit_advisor_budgets_the_pool_with_a_cuda_context_per_card(box, monkeypatch):
    out = _fit(monkeypatch)
    assert out["gpu_count"] == 2
    assert out["gpu_name"] == "RTX 4070 Ti + RTX 5060 Ti"
    assert out["vram_total_bytes"] == (12282 + 16311) * MIB
    assert out["reserve_bytes"] == 2 * vram_fit.DEFAULT_RESERVE_BYTES
    # someone else's share is the pool's used minus what Ollama holds
    assert out["vram_used_by_others_bytes"] == max(0, (1046 + 9263) * MIB - 8_100_000_000)
    assert out["budget_bytes"] == out["vram_total_bytes"] - out["reserve_bytes"] - out["vram_used_by_others_bytes"]
    # 17 GB of weights fit the 28 GB pool where they never fit one 12 GB card
    assert out["fits"] is True


def test_fit_advisor_on_one_card_is_unchanged(box, monkeypatch):
    box["gpu"] = ONE_CARD
    out = _fit(monkeypatch, model="qwen3.5:9b", file_size=6_600_000_000)
    assert out["gpu_count"] == 1 and out["gpu_name"] == "NVIDIA GeForce RTX 4070 Ti"
    assert out["reserve_bytes"] == vram_fit.DEFAULT_RESERVE_BYTES
    assert out["vram_total_bytes"] == 12282 * MIB


def test_fit_advisor_without_a_card_is_a_503(box, monkeypatch):
    from fastapi import HTTPException
    box["gpu"] = ""
    with pytest.raises(HTTPException) as e:
        _fit(monkeypatch)
    assert e.value.status_code == 503


def test_pool_name_comes_from_the_shared_helper():
    assert gpu_shared_memory.pool_name(["NVIDIA GeForce RTX 4070 Ti", "NVIDIA GeForce RTX 5060 Ti"]) == "RTX 4070 Ti + RTX 5060 Ti"
