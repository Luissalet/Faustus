"""Trace summaries expose observed usage without inventing missing counters."""
from src import llm_trace


def test_summary_retains_run_and_partial_usage(monkeypatch):
    usage = {"input_tokens": 0, "cost_usd": 0, "usage_source": "reported_engine",
             "cost_state": "known", "total_tokens": 12, "api_key": "secret"}
    monkeypatch.setattr(llm_trace, "_iter_records", lambda _: iter([
        {"seq": 1, "run_id": "run-a", "usage": usage},
        {"seq": 2, "run_id": "run-b", "usage": {"output_tokens": 4}},
        {"seq": 3}]))
    rows = llm_trace.list_calls("same-session")
    assert rows[0]["run_id"] == "run-a"
    assert rows[0]["usage"] == {k: v for k, v in usage.items() if k != "api_key"}
    assert rows[1]["run_id"] == "run-b" and rows[1]["usage"] == {"output_tokens": 4}
    assert rows[2]["run_id"] is None and rows[2]["usage"] == {}
    assert "output_tokens" not in rows[0]["usage"]
    assert "total_tokens" not in rows[1]["usage"]


def test_summary_rejects_malformed_usage_without_dropping_valid_fields(monkeypatch):
    monkeypatch.setattr(llm_trace, "_iter_records", lambda _: iter([
        {"usage": {"input_tokens": True, "output_tokens": -1, "total_tokens": float("inf"),
                   "cached_tokens": "secret", "reasoning_tokens": 2,
                   "cost_usd": float("nan"), "cost_state": "secret", "usage_source": []}},
        {"usage": "legacy-invalid"}]))
    rows = llm_trace.list_calls("s")
    assert rows[0]["usage"] == {"reasoning_tokens": 2}
    assert rows[1]["usage"] == {}


def test_summary_does_not_invent_cost_or_phase(monkeypatch):
    monkeypatch.setattr(llm_trace, "_iter_records", lambda _: iter([
        {"usage": {"input_tokens": 12, "output_tokens": 3, "cost_state": "unknown"},
         "request": {"messages": ["private"]}, "thinking_text": "private"}]))
    row = llm_trace.list_calls("s")[0]
    assert row["usage"] == {"input_tokens": 12, "output_tokens": 3, "cost_state": "unknown"}
    assert "request" not in row and "thinking_text" not in row and "phase" not in row


def test_summary_reads_usage_from_real_trace_file(tmp_path, monkeypatch):
    monkeypatch.setattr(llm_trace, "_traces_dir", lambda: str(tmp_path))
    llm_trace._write_line(llm_trace._log_path("temporary-session"), {
        "seq": 1, "run_id": "recorded-run", "duration_ms": 15.5,
        "usage": {"prompt_tokens": 9, "completion_tokens": 0, "cost_usd": 0.002,
                  "usage_source": "real", "cost_state": "known"}})
    full = llm_trace.get_call("temporary-session", 1)
    row = llm_trace.list_calls("temporary-session")[0]
    assert row["usage"] == full["usage"]
    assert row["run_id"] == full["run_id"] and row["duration_ms"] == 15.5
