"""StuckWatch (src/loop_breaker.py): monologue, repeated "request too long"
errors and a call that keeps failing. Unit tests for the counters, then the
loop driven with a scripted provider (same pattern as tests/test_advisor.py).
"""
import asyncio
import json

import pytest

import src.agent_loop as al
from src import loop_breaker as lb
from src.loop_breaker import StuckWatch, is_context_length_error, is_failed_result


# ------------------------------------------------------------- monologue --

def test_monologue_stops_after_three_text_only_rounds_and_a_tool_resets():
    w = StuckWatch()
    assert w.observe_round(tool_calls=0, has_text=True) == "none"
    assert w.observe_round(tool_calls=0, has_text=True) == "none"
    assert w.observe_round(tool_calls=1, has_text=True) == "none"      # reset
    assert w.monologue_streak == 0
    assert [w.observe_round(tool_calls=0, has_text=True) for _ in range(3)] == ["none", "none", "nudge"]
    # the nudge starts the count again; a second run of the same length stops the turn
    assert w.monologue_streak == 0
    assert [w.observe_round(tool_calls=0, has_text=True) for _ in range(3)] == ["none", "none", "stop"]


def test_empty_rounds_neither_count_nor_reset():
    w = StuckWatch()
    w.observe_round(tool_calls=0, has_text=True)
    assert w.observe_round(tool_calls=0, has_text=False) == "none"
    assert w.monologue_streak == 1


def test_limits_are_clamped_and_zero_switches_a_detector_off():
    off = StuckWatch(monologue_rounds=0, context_error_limit=0, failed_path_limit=0)
    assert all(off.observe_round(tool_calls=0, has_text=True) == "none" for _ in range(10))
    assert off.observe_provider_error("context length exceeded") == "compact"
    assert off.observe_provider_error("context length exceeded") == "compact"
    assert not any(off.observe_call("bash", "ls", True) for _ in range(10)) and not off.path_closed("bash", "ls")
    assert StuckWatch(monologue_rounds=1).monologue_rounds == 2
    assert StuckWatch(monologue_rounds=5).monologue_rounds == 5


def test_from_settings_reads_the_three_limits():
    values = {"agent_loop_breaker_monologue_rounds": 4, "agent_loop_breaker_context_error_limit": 3,
              "agent_loop_breaker_failed_path_limit": 5}
    w = StuckWatch.from_settings(lambda k, d=None: values.get(k, d))
    assert (w.monologue_rounds, w.context_error_limit, w.failed_path_limit) == (4, 3, 5)
    assert StuckWatch.from_settings(lambda k, d=None: "junk").monologue_rounds == 3


# --------------------------------------------------------- context errors --

@pytest.mark.parametrize("text", [
    "This model's maximum context length is 8192 tokens", "context_length_exceeded",
    "prompt is too long: 210000 tokens > 200000 maximum", "the request exceeds the available context size",
    "Input is too long for requested model", "la ventana de contexto es demasiado pequeña",
    {"error": "x", "error_class": "context_length_exceeded"},
])
def test_context_length_errors_are_recognised(text):
    assert is_context_length_error(text)


@pytest.mark.parametrize("text", ["rate limit exceeded", "invalid api key", "Model request failed", "", None,
                                  {"status": 400, "error": "bad request"}])
def test_other_errors_are_not(text):
    assert not is_context_length_error(text)


def test_first_context_error_compacts_the_second_in_a_row_stops():
    w = StuckWatch()
    assert w.observe_provider_error("context length exceeded") == "compact"
    assert w.observe_provider_error("context length exceeded") == "stop"
    w2 = StuckWatch()
    w2.observe_provider_error("context length exceeded")
    w2.observe_provider_ok()
    assert w2.observe_provider_error("context length exceeded") == "compact"
    w3 = StuckWatch()
    w3.observe_provider_error("context length exceeded")
    assert w3.observe_provider_error("rate limited") == "none" and w3.context_error_streak == 0


# ------------------------------------------------------------ failed path --

def test_failed_result_classification():
    assert is_failed_result({"error": "x"}) and is_failed_result({"exit_code": 1})
    assert not is_failed_result({"exit_code": 0}) and not is_failed_result({"error": "x", "blocked": True})
    assert not is_failed_result({"approval_required": True}) and not is_failed_result(None)
    assert not is_failed_result({"exit_code": True})


def test_same_call_failing_three_times_closes_that_path_only():
    w = StuckWatch()
    assert [w.observe_call("bash", "pytest -x", True) for _ in range(3)] == [False, False, True]
    assert w.path_closed("bash", "pytest -x")
    assert not w.path_closed("bash", "pytest -q"), "other arguments stay open"
    assert not w.path_closed("read_file", "pytest -x"), "other tools stay open"
    assert w.observe_call("bash", "pytest -x", True) is False, "it closes once"
    assert w.closed_paths_total == 1


def test_argument_spelling_does_not_hide_a_repeat_and_success_reopens():
    w = StuckWatch()
    w.observe_call("read_file", '{"path": "a", "x": 1}', True)
    w.observe_call("read_file", '{"x": 1, "path": "a"}', True)
    assert w.observe_call("read_file", '{"path":"a","x":1}', True) is True
    w.observe_call("read_file", '{"path":"a","x":1}', False)
    assert not w.path_closed("read_file", '{"path":"a","x":1}') and w.failures_for("read_file", '{"path":"a","x":1}') == 0


def test_tracked_paths_are_bounded():
    w = StuckWatch()
    for i in range(lb.MAX_TRACKED_PATHS * 3):
        w.observe_call("bash", f"cmd {i}", True)
    assert len(w._failures) <= lb.MAX_TRACKED_PATHS


# ------------------------------------------------------------ the loop -----

def _collect(gen):
    async def _go():
        return [c async for c in gen]
    return asyncio.run(_go())


def _events(chunks):
    out = []
    for c in chunks:
        if c.startswith("event: error\n"):
            c = c.split("\n", 1)[1]
        if c.startswith("data: ") and not c.startswith("data: [DONE]"):
            try:
                out.append(json.loads(c[6:]))
            except Exception:
                pass
    return out


def _patch(monkeypatch, tool_output=None, exec_log=None, settings=None):
    values = settings or {}
    monkeypatch.setattr(al, "get_setting", lambda key, default=None: values.get(key, default), raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)
    monkeypatch.setattr(al, "blocked_tools_for_owner", lambda owner: set(), raising=False)
    monkeypatch.setattr(al, "_agent_route_tool_mode", lambda *a, **k: (True, False, True), raising=False)

    async def _exec(block, *a, **k):
        if exec_log is not None:
            exec_log.append(block)
        out = tool_output(block) if callable(tool_output) else tool_output
        return (block.tool_type, out if out is not None else {"output": "ok", "exit_code": 0})
    monkeypatch.setattr(al, "execute_tool_block", _exec, raising=False)


def _native(name, args):
    return [f'data: {json.dumps({"type": "tool_calls", "calls": [{"name": name, "arguments": json.dumps(args)}]})}\n\n',
            f'data: {json.dumps({"type": "finish", "finish_reason": "tool_calls"})}\n\n']


def _text(t):
    return [f'data: {json.dumps({"delta": t})}\n\n', f'data: {json.dumps({"type": "finish", "finish_reason": "stop"})}\n\n']


def _error(message, status=400):
    return [f'event: error\ndata: {json.dumps({"error": message, "status": status})}\n\n']


def _drive(monkeypatch, script, tmp_path, *, max_rounds=8, user="Implement the fixes in the zip for the project",
           tools=None):
    """script: list (per provider call) of lists of SSE chunks; the last repeats."""
    seen = []

    async def _stream(_c, messages, **kwargs):
        seen.append([dict(m) for m in messages])
        for chunk in script[min(len(seen) - 1, len(script) - 1)]:
            yield chunk
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_llm_with_fallback", _stream, raising=False)
    chunks = _collect(al.stream_agent_loop(
        "http://x/v1", "m", [{"role": "user", "content": user}], workspace=str(tmp_path),
        max_rounds=max_rounds, relevant_tools=tools or {"read_file", "bash", "write_file"}))
    return seen, _events(chunks)


INTENT = "Let me check the logs"


def test_loop_nudges_then_stops_when_the_watch_says_so(monkeypatch, tmp_path):
    # Every continuing no-tool path the loop has is capped by its own
    # mechanism (one nudge each, two rejections, the intent supervisor), so a
    # real monologue of three rounds is rare; this drives the WIRING with the
    # counter forced: the first verdict is a nudge (the turn goes on with a
    # runtime note), the second a stop honoured at the top of the next round.
    _patch(monkeypatch)
    verdicts = iter(["nudge", "stop"])
    real = lb.StuckWatch.observe_round

    def _observe(self, *, tool_calls, has_text):
        real(self, tool_calls=tool_calls, has_text=has_text)
        return next(verdicts, "none") if not tool_calls else "none"
    monkeypatch.setattr(lb.StuckWatch, "observe_round", _observe)
    seen, events = _drive(monkeypatch, [_text("Reference context received.")], tmp_path, max_rounds=8)
    stops = [e for e in events if e.get("type") == "loop_breaker_stop" and e.get("trigger") == "monologue"]
    assert stops and stops[0]["round"] == 3, [e.get("type") for e in events]
    assert len(seen) == 2, "the nudged round ran, the stop came before a third model call"
    assert any("without calling a tool" in str(m.get("content")) for m in seen[1]), "round 2 carries the nudge"
    metrics = [e for e in events if e.get("type") == "metrics"][-1]["data"]
    assert [e["event"] for e in metrics["stuck_watch"]["events"]] == ["monologue_nudge", "monologue_stop"]


def test_monologue_nudge_asks_the_advisor_when_it_is_on(monkeypatch, tmp_path):
    from src import advisor
    _patch(monkeypatch)
    monkeypatch.setattr(advisor, "_setting", lambda key: True if key == "advisor_enabled" else advisor.DEFAULTS.get(key))
    asked = []

    async def _resolve(owner):
        return "spec", "http://adv/v1", "adv-model", {}

    async def _complete(url, model, messages, headers, *, max_tokens, overrides, on_usage=None):
        asked.append(messages)
        return "Do call read_file on the failing module, not another summary, because nothing has been read."
    monkeypatch.setattr(advisor, "_resolve", _resolve)
    monkeypatch.setattr(advisor, "_complete", _complete)
    verdicts = iter(["nudge"])
    real = lb.StuckWatch.observe_round

    def _observe(self, *, tool_calls, has_text):
        real(self, tool_calls=tool_calls, has_text=has_text)
        return next(verdicts, "none") if not tool_calls else "none"
    monkeypatch.setattr(lb.StuckWatch, "observe_round", _observe)
    seen, events = _drive(monkeypatch, [_text("Reference context received.")], tmp_path, max_rounds=3)
    adv = [e for e in events if e.get("type") == "advisor_advice" and e["trigger"] == "loop"]
    assert len(adv) == 1 and len(asked) == 1
    assert "without calling any tool" in json.dumps(asked[0])
    assert any("Runtime advisor note" in str(m.get("content")) for m in seen[1])
    assert not any("without calling a tool" in str(m.get("content")) for m in seen[1]), "advice replaces the generic nudge"


def test_a_long_answer_continued_after_a_token_cut_is_not_a_monologue(monkeypatch, tmp_path):
    _patch(monkeypatch)
    cut = lambda n: [f'data: {json.dumps({"delta": f"Section {n} of the long document. "})}\n\n',
                     f'data: {json.dumps({"type": "finish", "finish_reason": "length"})}\n\n']
    seen, events = _drive(monkeypatch, [cut(i) for i in range(5)] + [_text("The end.")], tmp_path,
                          max_rounds=8, user="Write a long document about the project")
    assert not any(e.get("type") == "loop_breaker_stop" and e.get("trigger") == "monologue" for e in events)


def test_loop_monologue_limit_can_be_switched_off(monkeypatch, tmp_path):
    _patch(monkeypatch, settings={"agent_loop_breaker_monologue_rounds": 0})
    w = StuckWatch.from_settings(lambda k, d=None: 0 if k == "agent_loop_breaker_monologue_rounds" else d)
    assert all(w.observe_round(tool_calls=0, has_text=True) == "none" for _ in range(9))


CTX_ERR = "This model's maximum context length is 8192 tokens, however you requested 9000 tokens"


def test_loop_first_context_error_compacts_harder_and_redoes_the_round(monkeypatch, tmp_path):
    _patch(monkeypatch)
    forced = []
    real = al.apply_midturn_pressure

    async def _spy(messages, **kw):
        if kw.get("force"):
            forced.append(kw)
        return await real(messages, **kw)
    monkeypatch.setattr(al, "apply_midturn_pressure", _spy)
    seen, events = _drive(monkeypatch, [_error(CTX_ERR), _text("Here is the answer.")], tmp_path,
                          user="What is two plus two?")
    assert len(forced) == 1, "exactly one forced compaction"
    assert len(seen) == 2, "the round was redone once"
    assert any(e.get("type") == "harness_check" and e.get("reason") == "context_overflow_compact" for e in events)
    assert not any(e.get("type") == "agent_terminal" for e in events)
    assert "Here is the answer." in json.dumps(events)


def test_loop_second_context_error_in_a_row_stops_with_a_clear_message(monkeypatch, tmp_path):
    _patch(monkeypatch)
    seen, events = _drive(monkeypatch, [_error(CTX_ERR)], tmp_path, user="What is two plus two?")
    assert len(seen) == 2, "one retry after compaction, then stop -- no third request"
    errs = [e for e in events if e.get("error_class") == "context_length_exceeded"]
    assert errs, events
    assert "context window" in errs[0]["error"] and "new chat" in errs[0]["error"]
    assert "maximum context length" in errs[0]["provider_error"]


def test_loop_other_errors_are_untouched(monkeypatch, tmp_path):
    _patch(monkeypatch)
    seen, events = _drive(monkeypatch, [_error("upstream exploded", 500)], tmp_path, user="What is two plus two?")
    assert len(seen) == 1
    assert not any(e.get("error_class") == "context_length_exceeded" for e in events)


def _a_fails(block):
    return {"error": "No such file: a.py", "exit_code": 1} if block.content.strip() == "a.py" else None


def _interleaved(n_fail_calls=4):
    # The same failing call with other work in between: the legacy round
    # breaker only sees consecutive repeats, so this is what the watch adds.
    script = []
    for i in range(n_fail_calls):
        script.append(_native("read_file", {"path": "a.py"}))
        script.append(_native("read_file", {"path": f"ok{i}.py"}))
    return script + [_text("I could not read a.py.")]


def test_loop_closes_a_path_after_three_failures_and_refuses_the_fourth_unrun(monkeypatch, tmp_path):
    executed = []
    _patch(monkeypatch, tool_output=_a_fails, exec_log=executed)
    seen, events = _drive(monkeypatch, _interleaved(), tmp_path, max_rounds=12, user="read the files", tools={"read_file"})
    a_calls = [b for b in executed if b.content.strip() == "a.py"]
    assert len(a_calls) == 3, "the fourth identical attempt never ran"
    flat = [str(m.get("content")) for r in seen for m in r]
    assert any("That path is closed" in c for c in flat)
    assert any("has already failed" in json.dumps(e) for e in events), "the refusal reaches the model as a blocked result"
    assert len([b for b in executed if b.content.strip().startswith("ok")]) == 4, "other arguments are untouched"
    metrics = [e for e in events if e.get("type") == "metrics"][-1]["data"]
    kinds = [x["event"] for x in metrics["stuck_watch"]["events"]]
    assert "failed_path_closed" in kinds and "failed_path_refused" in kinds


def test_loop_different_arguments_are_not_refused(monkeypatch, tmp_path):
    executed = []
    _patch(monkeypatch, tool_output={"error": "boom", "exit_code": 1}, exec_log=executed)
    script = [_native("read_file", {"path": f"f{i}.py"}) for i in range(5)] + [_text("done")]
    _drive(monkeypatch, script, tmp_path, max_rounds=8, user="read the files", tools={"read_file"})
    assert len([b for b in executed if b.tool_type == "read_file"]) == 5


def test_loop_failed_path_limit_off(monkeypatch, tmp_path):
    executed = []
    _patch(monkeypatch, tool_output=_a_fails, exec_log=executed,
           settings={"agent_loop_breaker_failed_path_limit": 0})
    _drive(monkeypatch, _interleaved(), tmp_path, max_rounds=12, user="read the files", tools={"read_file"})
    assert len([b for b in executed if b.content.strip() == "a.py"]) == 4
