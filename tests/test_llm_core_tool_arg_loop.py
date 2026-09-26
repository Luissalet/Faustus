"""A tool call whose streamed arguments loop on one passage is cut mid-stream.

Seen live: a local 27B repeated one sentence inside a `rationale` argument
until max_tokens (8192 tokens, 14 minutes) and nothing reached the screen,
because the server-side tool-call parser only releases argument text as
tool-call deltas, which the content/reasoning guards never look at."""
import json

from src import llm_core
from tests.test_llm_core_streaming import _drive, _sse

SENTENCE = "Los tres resuelven el problema de maquetado que no se debe reescribir. "


def test_detector_fires_on_a_prose_loop_at_the_end():
    args = '{"slate": {"rationale": "' + "Contexto inicial. " + SENTENCE * 12
    assert llm_core.tool_argument_loop(args)


def test_detector_ignores_short_or_non_prose_repetition():
    # a CSV-ish block of identical numeric rows is data, not a loop of prose
    rows = "1,2,3,4,5,6,7,8,9,10,11,12,13,14,15\n" * 40
    assert llm_core.tool_argument_loop('{"content": "' + rows) is None
    # a sentence repeated a few times is not enough
    assert llm_core.tool_argument_loop('{"text": "' + SENTENCE * 4) is None
    # varied prose
    varied = " ".join(f"Frase número {i} distinta de las demás por completo." for i in range(80))
    assert llm_core.tool_argument_loop('{"text": "' + varied) is None


def test_stream_is_cut_with_a_degenerate_error(monkeypatch):
    head = '{"action":"verify","slate":{"rationale":"'
    lines = [_sse({"tool_calls": [{"index": 0, "id": "c1", "type": "function",
                                   "function": {"name": "prior_art", "arguments": head}}]})]
    for _ in range(60):
        lines.append(_sse({"tool_calls": [{"index": 0, "function": {"arguments": SENTENCE}}]}))
    lines.append("data: [DONE]")
    events = _drive(monkeypatch, lines, model="local-27b")
    errors = [e for e in events if e.get("error_class") == llm_core.DEGENERATE_OUTPUT_ERROR_CLASS]
    assert errors, events[-3:]
    assert "prior_art" in errors[0]["error"]
    assert not any(e.get("type") == "tool_calls" for e in events)


def test_gibberish_in_tool_call_arguments_is_caught_from_the_first_chunks(monkeypatch):
    # §179: a broken model server ("////" or word-salad token soup) can emit
    # its garbage as tool-call argument text instead of plain content. The
    # server-side tool-call parser only releases that text as tool-call
    # deltas, which the loop-only check needs 1500+ chars to evaluate — this
    # must be cut long before that, on the same guard content/reasoning use.
    lines = [_sse({"tool_calls": [{"index": 0, "id": "c1", "type": "function",
                                   "function": {"name": "run_command", "arguments": '{"cmd": "'}}]})]
    for _ in range(20):
        lines.append(_sse({"tool_calls": [{"index": 0, "function": {"arguments": "/" * 10}}]}))
    lines.append("data: [DONE]")
    events = _drive(monkeypatch, lines, model="local-27b")
    errors = [e for e in events if e.get("error_class") == llm_core.DEGENERATE_OUTPUT_ERROR_CLASS]
    assert errors, events[-3:]
    assert "repeated" in errors[0]["error"]
    assert not any(e.get("type") == "tool_calls" for e in events)
    # Cut well before the loop-only check's 1500-char warm-up window would
    # even start looking — this is the "very first tokens" guard, not that one.
    assert sum(len(ln) for ln in lines) > 200
