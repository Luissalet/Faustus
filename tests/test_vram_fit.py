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
