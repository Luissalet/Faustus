"""A period's usage across every chat."""
from datetime import datetime

from src import usage_recap


def _rows():
    t = datetime(2026, 9, 20, 10, 0)
    return [
        {"metadata": {"model": "qwen", "input_tokens": 1000, "output_tokens": 100, "agent_rounds": 3,
                      "tool_events": [{"tool": "read_file"}, {"tool": "read_file"}, {"tool": "bash"}],
                      "prompt_cache": {"rounds": 3, "processed": 200, "cached": 800}},
         "ts": t, "session_id": "a", "endpoint_url": "http://127.0.0.1:8081/v1"},
        {"metadata": {"model": "claude-sonnet-5", "input_tokens": 500, "output_tokens": 50, "cost_usd": 0.01},
         "ts": t, "session_id": "b", "endpoint_url": "https://api.anthropic.com/v1/messages"},
        {"metadata": {"note": "no metrics"}, "ts": t, "session_id": "c", "endpoint_url": ""},
    ]


def test_recap_folds_every_chat():
    d = usage_recap.recap("admin", 30, rows=_rows())
    assert d["chats"] == 2 and d["total"]["turns"] == 2 and d["total"]["input_tokens"] == 1500
    assert d["turns_local"] == 1 and d["turns_hosted"] == 1
    assert d["top_tools"][0] == ("read_file", 2)
    assert d["busiest_days"][0] == ("2026-09-20", 2)
    md = usage_recap.render(d)
    assert "2 turns in 2 chats" in md and "read_file 2" in md


def test_empty_period():
    assert "No turns" in usage_recap.render(usage_recap.recap("admin", 7, rows=[]))
