from src.session_usage import summarize


def test_usage_folds_turns_per_model_with_cache():
    msgs = [
        {"role": "user", "metadata": {}},
        {"role": "assistant", "metadata": {"model": "qwen", "input_tokens": 1000, "output_tokens": 50,
                                           "agent_rounds": 3, "tool_calls": 4, "total_time": 12.5,
                                           "prompt_cache": {"processed": 200, "cached": 800, "lost_rounds": 0},
                                           "context_length": 235000, "context_percent": 2.1,
                                           "request_context_tokens": 5000}},
        {"role": "assistant", "metadata": {"model": "claude-sonnet-5", "input_tokens": 300, "output_tokens": 70,
                                           "agent_rounds": 1, "cost_usd": 0.0123}},
        {"role": "assistant", "metadata": {"note": "no metrics"}},
    ]
    out = summarize(msgs)
    t = out["total"]
    assert t["turns"] == 2 and t["input_tokens"] == 1300 and t["steps"] == 4 and t["tool_calls"] == 4
    assert t["cache_hit_percent"] == 80.0 and t["cost_usd"] == 0.0123
    assert set(out["by_model"]) == {"qwen", "claude-sonnet-5"}
    assert out["context"]["context_length"] == 235000


def test_empty_chat():
    assert summarize([])["total"] == {"turns": 0}
