"""Time as a construct: every tool result the model reads starts with wall time.

The observation behind it: a model that never sees durations treats a
40-minute command and a 40 ms one alike, so it re-runs slow things "to see if
they changed" and curls a server 0.2 s after launching it.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from src import tool_clock as tc
from src.tool_execution import format_tool_result


@pytest.fixture(autouse=True)
def _clock_on(monkeypatch):
    monkeypatch.setattr(tc, "enabled", lambda: True)
    yield


def test_fmt_duration_reads_like_a_person_would_say_it():
    assert tc.fmt_duration(0.3) == "0.3s"
    assert tc.fmt_duration(12.4) == "12s"
    assert tc.fmt_duration(125) == "2m 05s"
    assert tc.fmt_duration(3600 * 1.25) == "1h 15m"
    assert tc.fmt_duration(None) == "?"


def test_stamp_records_elapsed_and_turn_clock():
    tok = tc.begin_turn()
    try:
        tc.set_round(3)
        t0 = time.monotonic() - 2.0
        res = tc.stamp({"output": "hi", "exit_code": 0}, t0)
        assert res == {"output": "hi", "exit_code": 0}  # the dict itself is untouched
        t = tc.timing_of(res)
        assert 1900 <= t["elapsed_ms"] <= 4000
        assert t["turn_elapsed_ms"] is not None and t["turn_elapsed_ms"] >= 0
        assert t["round"] == 3
        assert "T" in t["started_at"]
    finally:
        tc.end_turn(tok)


def test_stamp_leaves_non_dict_results_alone():
    assert tc.stamp("text", time.monotonic()) == "text"
    assert tc.stamp(None, time.monotonic()) is None


def test_formatted_result_starts_with_the_clock_line():
    res = tc.stamp({"output": "ok", "exit_code": 0}, time.monotonic() - 12.4)
    text = format_tool_result("bash", res)
    lines = text.splitlines()
    assert lines[0] == "### bash"
    assert lines[1].startswith("⏱ took 12s")
    assert "started " in lines[1]
    # Read once: a second format of the same dict carries no stale clock.
    assert "⏱" not in format_tool_result("bash", res)
    assert "```\nok\n```" in text


def test_slow_call_gets_a_plain_words_warning():
    res = tc.stamp({"output": "", "exit_code": 0}, time.monotonic() - 5 * 60)
    text = format_tool_result("bash", res)
    assert "took 5m 00s" in text
    assert "Do not run it again" in text
    assert "#!bg" in text


def test_fast_call_has_no_warning():
    res = tc.stamp({"output": "", "exit_code": 0}, time.monotonic() - 0.2)
    text = format_tool_result("bash", res)
    assert "Do not run it again" not in text


def test_clock_line_absent_when_setting_off(monkeypatch):
    monkeypatch.setattr(tc, "enabled", lambda: False)
    res = tc.stamp({"output": "ok", "exit_code": 0}, time.monotonic() - 3)
    assert "⏱" not in format_tool_result("bash", res)


def test_unstamped_result_formats_exactly_as_before():
    assert format_tool_result("bash", {"output": "ok", "exit_code": 0}) == "### bash\n```\nok\n```"


def test_attach_carries_timing_to_a_replacement_dict():
    res = tc.stamp({"output": "x" * 10}, time.monotonic() - 2)
    replacement = {"output": "x…", "artifact_id": "a1"}
    tc.attach(replacement, tc.timing_of(res))
    assert tc.timing_of(replacement)["elapsed_ms"] >= 1900


def test_sse_fields_carry_duration_and_turn_clock():
    tok = tc.begin_turn()
    try:
        res = tc.stamp({"output": ""}, time.monotonic() - 1)
        f = tc.sse_fields(res)
        assert 900 <= f["duration_ms"] <= 3000
        assert "turn_elapsed_ms" in f
    finally:
        tc.end_turn(tok)
    assert tc.sse_fields({"output": ""}) == {}


def test_turn_clock_is_per_task():
    """Two concurrent turns (a chat and a worker) each read their own start."""
    seen = {}

    async def turn(name, delay):
        tok = tc.begin_turn()
        await asyncio.sleep(delay)
        seen[name] = tc.turn_elapsed_s()
        tc.end_turn(tok)

    async def main():
        await asyncio.gather(turn("a", 0.05), turn("b", 0.2))

    asyncio.run(main())
    assert seen["a"] < seen["b"]
    assert tc.turn_elapsed_s() is None


def test_execute_tool_block_stamps_the_result(monkeypatch, tmp_path):
    """The stamp happens at the single funnel every caller uses."""
    from src import tool_execution as te

    class _Block:
        tool_type = "read_file"
        content = '{"path": "x.txt"}'

    async def fake_impl(block, **kw):
        await asyncio.sleep(0.05)
        return "read_file x.txt", {"content": "hello", "size": 5}

    monkeypatch.setattr(te, "_execute_tool_block_impl", fake_impl)
    out = asyncio.run(te.execute_tool_block(
        _Block(), session_id="s", disabled_tools=set(), owner="o",
        security_context=te.NO_TOOL_SECURITY_CONTEXT,
    ))
    assert isinstance(out, tuple)
    assert set(out[1].keys()) == {"content", "size"}  # CALL-05: byte-for-byte
    t = tc.timing_of(out[1])
    assert t["elapsed_ms"] >= 40
    assert "took " in format_tool_result(out[0], out[1])
