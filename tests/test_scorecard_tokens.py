"""Tokens per trajectory on scorecard rows and their per-model means."""
from __future__ import annotations

from src import scorecard


def _entry(**kw):
    base = dict(session_id="s", model="m", endpoint_label=None, workspace="/w", user_text="t",
                duration_s=10, rounds=4, harness={"tool_calls": 8, "stop_reason": "complete"})
    base.update(kw)
    return scorecard.build_entry(**base)


def test_scorecard_entry_carries_token_fields():
    e = _entry(input_tokens=90_000, total_tokens=100_000, output_tokens=10_000,
               context_ops={"pins": 1, "drops": 2, "notes": 0, "tokens_freed": 5000, "status": 1, "unpins": 0})
    assert e["input_tokens"] == 90_000 and e["total_tokens"] == 100_000
    assert e["tokens_per_tool_call"] == 12_500.0 and e["tokens_per_round"] == 25_000.0
    assert e["context_ops"]["drops"] == 2
    e2 = _entry(input_tokens=1000, output_tokens=500)
    assert e2["total_tokens"] == 1500
    e3 = _entry()
    assert "total_tokens" not in e3 and "tokens_per_round" not in e3 and "context_ops" not in e3


def test_scorecard_aggregate_exposes_token_means():
    rows = scorecard.aggregate([
        _entry(input_tokens=1000, total_tokens=2000),
        _entry(input_tokens=3000, total_tokens=4000),
        _entry(),   # an older row without token fields does not count
    ])
    (row,) = rows
    assert row["avg_input_tokens"] == 2000.0
    assert row["avg_total_tokens"] == 3000.0
    assert row["avg_tokens_per_tool_call"] == 375.0
    assert row["avg_tokens_per_round"] == 750.0
    assert scorecard.aggregate([_entry()])[0]["avg_total_tokens"] is None
