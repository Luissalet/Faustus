"""Tests for scripts/eval_freshness.py — SSE parsing and verdict logic only.

No network: this exercises iter_sse_events/evaluate_stream/verdict_for with
canned event streams, the way the battery itself would see them from a real
server. The battery's live run (main()) needs a running Faustus instance and
is exercised manually / in CI against a real deployment, not here.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "eval_freshness.py"

_spec = importlib.util.spec_from_file_location("eval_freshness", SCRIPT_PATH)
eval_freshness = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("eval_freshness", eval_freshness)
_spec.loader.exec_module(eval_freshness)  # type: ignore[union-attr]

iter_sse_events = eval_freshness.iter_sse_events
evaluate_stream = eval_freshness.evaluate_stream
verdict_for = eval_freshness.verdict_for
QuestionResult = eval_freshness.QuestionResult
build_report = eval_freshness.build_report


def _sse_lines(events):
    """Turn a list of (dict | '[DONE]') into the raw line stream
    requests.iter_lines(decode_unicode=True) would hand back."""
    lines = []
    for ev in events:
        if ev == "[DONE]":
            lines.append("data: [DONE]")
        else:
            lines.append("data: " + json.dumps(ev))
        lines.append("")  # blank line between SSE frames
    return lines


def test_iter_sse_events_parses_and_stops_at_done():
    lines = _sse_lines([
        {"delta": "Hello"},
        {"type": "tool_start", "tool": "web_search"},
        "[DONE]",
        {"delta": "never reached"},
    ])
    events = list(iter_sse_events(lines))
    assert events[0] == {"delta": "Hello"}
    assert events[1] == {"type": "tool_start", "tool": "web_search"}
    assert events[2] == {"done": True}
    # Everything after [DONE] is still yielded by the parser (evaluate_stream
    # is what stops consuming at "done"); iter_sse_events itself is a dumb
    # line-by-line parser.
    assert len(events) == 4


def test_iter_sse_events_skips_malformed_json():
    lines = ["data: {not valid json", "", "data: " + json.dumps({"delta": "ok"}), ""]
    events = list(iter_sse_events(lines))
    assert events == [{"delta": "ok"}]


def test_evaluate_stream_detects_search_and_sources():
    events = [
        {"delta": "Let me check "},
        {"type": "tool_start", "tool": "web_search", "command": "real madrid last match result"},
        {"type": "tool_output", "tool": "web_search", "output": "..."},
        {"type": "web_sources", "data": [{"title": "A", "url": "https://marca.com/a"},
                                          {"title": "B", "url": "https://as.com/b"}]},
        {"delta": "Real Madrid won 2-1."},
        {"done": True},
    ]
    searched, refused, sources, text = evaluate_stream(events)
    assert searched is True
    assert refused is False
    assert sources == 2
    assert "Real Madrid won 2-1." in text


def test_evaluate_stream_detects_refusal_without_search():
    events = [
        {"delta": "I don't have access to real-time information."},
        {"done": True},
    ]
    searched, refused, sources, text = evaluate_stream(events)
    assert searched is False
    assert refused is True
    assert sources == 0


def test_evaluate_stream_spanish_refusal_phrase():
    events = [{"delta": "No tengo acceso a internet en este momento."}, {"done": True}]
    searched, refused, _sources, _text = evaluate_stream(events)
    assert searched is False
    assert refused is True


def test_evaluate_stream_no_search_no_refusal_for_timeless_answer():
    events = [
        {"delta": "You can sort a list in Python with sorted(my_list)."},
        {"done": True},
    ]
    searched, refused, sources, _text = evaluate_stream(events)
    assert searched is False
    assert refused is False
    assert sources == 0


def test_evaluate_stream_ignores_events_after_done():
    events = [
        {"delta": "answer"},
        {"done": True},
        {"type": "tool_start", "tool": "web_search"},  # must not count
    ]
    searched, _refused, _sources, _text = evaluate_stream(events)
    assert searched is False


def test_verdict_time_sensitive_pass():
    r = QuestionResult(question="who won", control=False, searched=True, refused=False, seconds=2.0, sources=3)
    ok, reason = verdict_for(r)
    assert ok is True
    assert reason == "ok"


def test_verdict_time_sensitive_not_searched_fails():
    r = QuestionResult(question="who won", control=False, searched=False, refused=False, seconds=2.0, sources=0)
    ok, reason = verdict_for(r)
    assert ok is False
    assert "not searched" in reason


def test_verdict_time_sensitive_refused_fails_even_if_searched():
    r = QuestionResult(question="who won", control=False, searched=True, refused=True, seconds=2.0, sources=1)
    ok, reason = verdict_for(r)
    assert ok is False
    assert "refusal" in reason


def test_verdict_control_not_searched_passes():
    r = QuestionResult(question="sort a list", control=True, searched=False, refused=False, seconds=1.0, sources=0)
    ok, _reason = verdict_for(r)
    assert ok is True


def test_verdict_control_searched_fails():
    r = QuestionResult(question="sort a list", control=True, searched=True, refused=False, seconds=1.0, sources=2)
    ok, reason = verdict_for(r)
    assert ok is False
    assert "control" in reason


def test_verdict_error_fails():
    r = QuestionResult(question="x", control=False, searched=False, refused=False, seconds=0.1, sources=0, error="timeout")
    ok, reason = verdict_for(r)
    assert ok is False
    assert "error" in reason


def test_build_report_contains_table_and_summary():
    results = [
        QuestionResult(question="who won Q", control=False, searched=True, refused=False, seconds=2.5, sources=3),
        QuestionResult(question="sort a list", control=True, searched=False, refused=False, seconds=0.8, sources=0),
        QuestionResult(question="stale question", control=False, searched=False, refused=True, seconds=1.1, sources=0),
    ]
    report = build_report(results)
    assert "# Freshness battery" in report
    assert "who won Q" in report
    assert "PASS" in report
    assert "FAIL" in report
    assert "2/3 passed." in report


def test_battery_has_time_sensitive_and_control_questions():
    assert len(eval_freshness.TIME_SENSITIVE_QUESTIONS) == 12
    assert len(eval_freshness.TIMELESS_CONTROL_QUESTIONS) == 4
    # Bilingual coverage.
    joined = " ".join(eval_freshness.TIME_SENSITIVE_QUESTIONS)
    assert any(c in joined for c in "áéíóúñ¿")
