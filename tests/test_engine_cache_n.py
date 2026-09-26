"""llama-server timings keep cache_n (prompt tokens reused from its cache)."""


def test_llamacpp_timings_keep_cache_n():
    from src.llm_core import _llamacpp_engine_timings
    out = _llamacpp_engine_timings({"prompt_n": 120, "cache_n": 22000, "prompt_ms": 150.0,
                                    "predicted_n": 40, "predicted_ms": 4000.0})
    assert out["cache_n"] == 22000 and out["prompt_n"] == 120
    assert _llamacpp_engine_timings({"prompt_n": 5})["cache_n"] is None
