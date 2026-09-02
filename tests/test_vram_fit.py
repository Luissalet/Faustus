"""The arithmetic behind "Fit to VRAM"."""
import pytest

from src import vram_fit

GB = 1024 ** 3
MB = 1024 ** 2


def test_measured_kv_per_token():
    # qwen3.5:9b as `ollama ps` reports it: 7566262270 bytes loaded at 65536
    # tokens, from a 6594474711 byte file.
    kv = vram_fit.kv_bytes_per_token_measured(7566262270, 6594474711, 65536)
    assert kv is not None
    assert 14000 < kv < 15500          # ~14.8 KB per token
    assert vram_fit.kv_bytes_per_token_measured(0, 1, 1) is None
    assert vram_fit.kv_bytes_per_token_measured(100, 200, 1024) is None  # negative


def test_estimated_kv_flags_missing_gqa_and_hybrid_layers():
    info = {
        "general.architecture": "qwen35",
        "qwen35.block_count": 32,
        "qwen35.attention.head_count": 16,
        "qwen35.attention.head_count_kv": "",     # absent in the real metadata
        "qwen35.attention.key_length": 256,
        "qwen35.attention.value_length": 256,
        "qwen35.full_attention_interval": 4,
        "qwen35.ssm.state_size": 128,
    }
    kv, note = vram_fit.kv_bytes_per_token_estimated(info)
    # 8 attention blocks (32/4), not 32.
    assert kv == 8 * 16 * (256 + 256) * 2
    assert "upper bound" in note and "head_count_kv" in note


def test_estimated_kv_without_metadata():
    assert vram_fit.kv_bytes_per_token_estimated({})[0] is None
    assert vram_fit.kv_bytes_per_token_estimated({"general.architecture": "x"})[0] is None


def _plan(**kw):
    base = dict(vram_total_bytes=12 * GB, file_size_bytes=6 * GB, n_layers=32,
                kv_bytes_per_token=14828.0, kv_source="measured", current_ctx=32768,
                max_ctx=262144)
    base.update(kw)
    return vram_fit.plan(**base)


def test_fits_and_never_raises_the_context_by_itself():
    p = _plan()
    assert p["fits"] is True and p["num_gpu"] is None
    assert p["num_ctx"] == 32768                  # asked for 32k, keeps 32k
    assert p["max_ctx_that_fits"] >= 65536        # but says there is room
    assert p["kv_cache_type"] is None


def test_target_context_is_honoured_when_it_fits():
    p = _plan(target_ctx=65536)
    assert p["num_ctx"] == 65536 and p["fits"] is True


def test_context_shrinks_before_layers_move_to_the_cpu():
    # A KV cache big enough that 32k does not fit but a smaller window does.
    p = _plan(kv_bytes_per_token=200000.0)
    assert p["fits"] is True
    assert p["num_ctx"] < 32768
    assert p["num_gpu"] is None
    assert any("Context" in s for s in p["steps"])


def test_q8_cache_is_tried_before_offloading():
    # Tight enough that even the smallest context does not fit with an f16 KV
    # cache, but does once the cache is halved — the layers must stay put.
    p = _plan(file_size_bytes=10 * GB, kv_bytes_per_token=400000.0)
    assert p["fits"] is True and p["kv_cache_type"] == "q8_0"


def test_partial_offload_when_the_model_is_too_big():
    p = _plan(file_size_bytes=17 * GB, n_layers=64)
    assert p["fits"] is False
    assert 0 < p["num_gpu"] < 65
    assert p["num_ctx"] <= 8192
    assert any("CPU" in s for s in p["steps"])


def test_unreliable_kv_says_so_when_it_matters():
    p = _plan(file_size_bytes=17 * GB, n_layers=64, kv_source="estimated", kv_reliable=False)
    assert any("ceiling" in s for s in p["steps"])
    p2 = _plan(file_size_bytes=17 * GB, n_layers=64)
    assert not any("ceiling" in s for s in p2["steps"])


def test_no_room_at_all():
    p = _plan(vram_used_by_others_bytes=12 * GB)
    assert p["fits"] is False and p["num_gpu"] == 0
    assert "free that first" in p["steps"][0]


def test_gpu_overhead_only_when_someone_else_is_on_the_card():
    assert _plan()["gpu_overhead_bytes"] == 0
    assert _plan(vram_used_by_others_bytes=2 * GB)["gpu_overhead_bytes"] == 512 * MB


def test_max_ctx_caps_the_ladder():
    p = _plan(max_ctx=8192, current_ctx=8192)
    assert p["num_ctx"] <= 8192


def test_plan_without_kv_information_still_answers():
    p = _plan(kv_bytes_per_token=None, kv_source="unknown", file_size_bytes=17 * GB, n_layers=64)
    assert p["fits"] is False and isinstance(p["num_gpu"], int)
    assert p["kv_cache_type"] is None


# ── The pool: two cards, one budget each and one for both ───────────────────
#
# The reference box after the second card: an RTX 4070 Ti (12282 MiB, GPU 0)
# next to an RTX 5060 Ti (16311 MiB, GPU 1). Ollama 0.33 puts a model on the
# card with the most free memory and splits one that fits no single card.

MIB = MB
RESERVE = vram_fit.DEFAULT_RESERVE_BYTES

_TWO = {
    "supported": True, "name": "RTX 4070 Ti + RTX 5060 Ti", "count": 2,
    "total": (12282 + 16311) * MIB, "used": (1046 + 9263) * MIB, "free": (12282 - 1046 + 16311 - 9263) * MIB,
    "gpus": [
        {"index": 0, "name": "NVIDIA GeForce RTX 4070 Ti", "uuid": "GPU-5ab7", "total": 12282 * MIB, "used": 1046 * MIB, "free": 11236 * MIB},
        {"index": 1, "name": "NVIDIA GeForce RTX 5060 Ti", "uuid": "GPU-15d1", "total": 16311 * MIB, "used": 9263 * MIB, "free": 7048 * MIB},
    ],
}
_ONE = {"supported": True, "name": "NVIDIA GeForce RTX 4070 Ti", "total": 12282 * MIB, "used": 8700 * MIB, "free": 3582 * MIB}
_HELD = 8_100_000_000          # qwen3.5:9b as `ollama ps` reports it
_SINGLE_ON_1 = {"qwen3.5:9b": {"gpus": [1], "per_gpu": [{"index": 1, "bytes": 8823 * MIB}], "placement": "single", "pid": 15948}}


def test_one_card_block_is_exactly_the_old_arithmetic():
    b = vram_fit.pool_budgets(_ONE, held_by_runner_bytes=_HELD, others_bytes=8700 * MIB - _HELD)
    assert b["count"] == 1 and b["name"] == "NVIDIA GeForce RTX 4070 Ti"
    assert b["reserve_bytes"] == RESERVE
    assert b["other_bytes"] == 8700 * MIB - _HELD
    assert b["budget_bytes"] == 12282 * MIB - RESERVE - (8700 * MIB - _HELD)
    assert b["clean_budget_bytes"] == 12282 * MIB - RESERVE
    # the one card IS the pool, so "largest single" is the pool budget
    assert b["largest_single_budget_bytes"] == b["budget_bytes"]
    assert b["largest_single_clean_budget_bytes"] == b["clean_budget_bytes"]
    (card,) = b["gpus"]
    assert card["index"] == 0 and card["total_bytes"] == 12282 * MIB
    assert card["models_bytes"] == _HELD and card["other_bytes"] == b["other_bytes"]
    assert card["budget_bytes"] == b["budget_bytes"]


def test_two_cards_budget_the_pool_with_a_reserve_per_card():
    others = (1046 + 9263) * MIB - _HELD
    b = vram_fit.pool_budgets(_TWO, held_by_runner_bytes=_HELD, others_bytes=others, placements=_SINGLE_ON_1)
    assert b["count"] == 2 and b["reserve_bytes"] == 2 * RESERVE and b["reserve_per_gpu_bytes"] == RESERVE
    assert b["total_bytes"] == (12282 + 16311) * MIB
    assert b["budget_bytes"] == (12282 + 16311) * MIB - 2 * RESERVE - others
    assert b["clean_budget_bytes"] == (12282 + 16311) * MIB - 2 * RESERVE


def test_each_card_knows_its_models_and_its_own_budget():
    others = (1046 + 9263) * MIB - _HELD
    b = vram_fit.pool_budgets(_TWO, held_by_runner_bytes=_HELD, others_bytes=others, placements=_SINGLE_ON_1)
    g0, g1 = b["gpus"]
    assert g1["models"] == ["qwen3.5:9b"] and g1["models_bytes"] == 8823 * MIB
    assert g1["other_bytes"] == (9263 - 8823) * MIB
    assert g1["budget_bytes"] == 16311 * MIB - RESERVE - (9263 - 8823) * MIB
    assert g0["models"] == [] and g0["models_bytes"] == 0
    assert g0["other_bytes"] == 1046 * MIB                     # the desktop, a browser
    assert g0["budget_bytes"] == 12282 * MIB - RESERVE - 1046 * MIB
    assert g0["clean_budget_bytes"] == 12282 * MIB - RESERVE and g1["clean_budget_bytes"] == 16311 * MIB - RESERVE
    assert b["largest_single_budget_bytes"] == g1["budget_bytes"]
    assert b["largest_single_clean_budget_bytes"] == g1["clean_budget_bytes"]


def test_a_split_model_is_charged_to_both_cards():
    held = 17_000_000_000
    split = {"qwen3.8:27b-q4_K_M": {"gpus": [0, 1], "per_gpu": [{"index": 0, "bytes": 8704 * MIB}, {"index": 1, "bytes": 10445 * MIB}],
                                    "placement": "split", "pid": 15948}}
    two = dict(_TWO, used=(9750 + 10886) * MIB, gpus=[dict(_TWO["gpus"][0], used=9750 * MIB), dict(_TWO["gpus"][1], used=10886 * MIB)])
    b = vram_fit.pool_budgets(two, held_by_runner_bytes=held, others_bytes=(9750 + 10886) * MIB - held, placements=split)
    g0, g1 = b["gpus"]
    assert g0["models"] == ["qwen3.8:27b-q4_K_M"] == g1["models"]
    assert g0["models_bytes"] == 8704 * MIB and g1["models_bytes"] == 10445 * MIB
    assert g0["other_bytes"] == (9750 - 8704) * MIB and g1["other_bytes"] == (10886 - 10445) * MIB


def test_unmeasured_cards_say_so_and_share_the_pools_others_pro_rata():
    """Placement failed (no nvidia-smi compute-apps, no counters): nobody
    knows which card holds the 8 GB. Neither card may claim it is empty."""
    others = (1046 + 9263) * MIB - _HELD
    b = vram_fit.pool_budgets(_TWO, held_by_runner_bytes=_HELD, others_bytes=others, placements={})
    g0, g1 = b["gpus"]
    assert g0["models_bytes"] is None and g1["models_bytes"] is None
    assert g0["other_bytes"] + g1["other_bytes"] == pytest.approx(others, abs=2)
    assert g0["other_bytes"] < g1["other_bytes"]                # weighted by what each holds
    assert g0["other_bytes"] <= 1046 * MIB and g1["other_bytes"] <= 9263 * MIB
    # a model placed "unknown" leaves the empty-looking card unknown too
    unknown = {"qwen3.5:9b": {"gpus": [], "per_gpu": [], "placement": "unknown", "pid": None}}
    b = vram_fit.pool_budgets(_TWO, held_by_runner_bytes=_HELD, others_bytes=others, placements=unknown)
    assert all(g["models_bytes"] is None for g in b["gpus"])
    # …whereas a CPU-only model is placed: the cards really are empty of it
    cpu = {"big:70b": {"gpus": [], "per_gpu": [], "placement": "cpu", "pid": 1}}
    b = vram_fit.pool_budgets(_TWO, held_by_runner_bytes=0, others_bytes=(1046 + 9263) * MIB, placements=cpu)
    assert [g["models_bytes"] for g in b["gpus"]] == [0, 0]


def test_a_card_with_one_unmeasured_model_is_unmeasured():
    part = {"a": {"gpus": [1], "per_gpu": [{"index": 1, "bytes": 4 * GB}], "placement": "single", "pid": 1},
            "b": {"gpus": [1], "per_gpu": [{"index": 1, "bytes": None}], "placement": "single", "pid": 2}}
    b = vram_fit.pool_budgets(_TWO, held_by_runner_bytes=_HELD, others_bytes=0, placements=part)
    assert b["gpus"][1]["models"] == ["a", "b"] and b["gpus"][1]["models_bytes"] is None


def test_nothing_loaded_means_every_card_is_known_empty():
    b = vram_fit.pool_budgets(_TWO, held_by_runner_bytes=0, others_bytes=(1046 + 9263) * MIB)
    assert [g["models_bytes"] for g in b["gpus"]] == [0, 0]
    assert [g["other_bytes"] for g in b["gpus"]] == [1046 * MIB, 9263 * MIB]


def test_needs_split_is_between_the_largest_card_and_the_pool():
    b = vram_fit.pool_budgets(_TWO, held_by_runner_bytes=0, others_bytes=(1046 + 9263) * MIB)
    single = b["largest_single_budget_bytes"]
    pool = b["budget_bytes"]
    assert single < pool
    assert vram_fit.needs_split(single - 1, b) is False           # fits one card
    assert vram_fit.needs_split(single + 1, b) is True            # only the pool holds it
    assert vram_fit.needs_split(pool, b) is True
    assert vram_fit.needs_split(pool + 1, b) is False             # over: not a split, a spill
    assert vram_fit.needs_split(0, b) is False
    # Discover fits against the clean budgets
    clean_single = b["largest_single_clean_budget_bytes"]
    assert vram_fit.needs_split(clean_single + 1, b, clean=True) is True
    # one card never splits
    one = vram_fit.pool_budgets(_ONE, held_by_runner_bytes=0, others_bytes=0)
    assert vram_fit.needs_split(one["budget_bytes"] - 1, one) is False
