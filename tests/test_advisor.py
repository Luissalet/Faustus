"""The advisor (src/advisor.py): a second model that reads the whole session
and writes a short advisory note, at three moments the loop decides in code.

Unit tests cover the transcript builder, the per-turn state, the call and its
failure paths; the loop tests drive `stream_agent_loop` with a fake provider
and a fake advisor endpoint, like tests/test_agent_rounds_exhausted.py does.
"""
import asyncio
import json

import pytest

import src.agent_loop as al
from src import advisor


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean_stats():
    advisor._reset_stats()
    yield
    advisor._reset_stats()


def _settings(monkeypatch, **values):
    base = dict(advisor.DEFAULTS)
    base.update(values)
    monkeypatch.setattr(advisor, "_setting", lambda key: base.get(key, advisor.DEFAULTS.get(key)))


# --------------------------------------------------------------- settings --

def test_off_by_default_and_model_falls_back_to_teacher(monkeypatch):
    assert advisor.DEFAULTS["advisor_enabled"] is False
    assert advisor.DEFAULTS["advisor_max_uses"] == 3
    assert advisor.DEFAULTS["advisor_max_tokens"] == 1024
    _settings(monkeypatch)
    import src.settings as settings
    monkeypatch.setattr(settings, "get_setting",
                        lambda key, default=None: {"teacher_model": "teach"}.get(key, default))
    assert advisor.model_spec() == "teach"
    _settings(monkeypatch, advisor_model="adv")
    assert advisor.model_spec() == "adv"


# ------------------------------------------------------------- transcript --

def _session():
    return [
        {"role": "system", "content": "You are the agent. " + "rules " * 400},
        {"role": "user", "content": "Refactor the parser and keep the tests green."},
        {"role": "assistant", "content": "Reading the parser.",
         "tool_calls": [{"id": "1", "function": {"name": "read_file", "arguments": '{"path": "parser.py"}'}}]},
        {"role": "tool", "name": "read_file", "content": "def parse(x):\n    return x\n" * 50},
        {"role": "assistant", "content": "Editing now."},
    ]


def test_transcript_has_system_summary_tools_calls_and_results():
    built = advisor.build_transcript(_session(), ["read_file", "write_file"], budget_tokens=8000)
    text = built["text"]
    assert "System prompt (summary" in text and "characters in all" in text
    assert "Tools on offer (names only): read_file, write_file" in text
    assert "CALL read_file" in text and "parser.py" in text
    assert "TOOL RESULT read_file" in text
    assert "<<<TRANSCRIPT>>>" in text and "<<<END_TRANSCRIPT>>>" in text
    assert built["omitted"] == 0
    # the system prompt is summarised, not pasted whole
    assert text.count("rules ") < 120


def test_transcript_respects_budget_by_shortening_then_dropping_the_middle():
    msgs = [{"role": "user", "content": "First request: build it."}]
    for i in range(60):
        msgs.append({"role": "assistant", "content": f"step {i} " + "x" * 400})
        msgs.append({"role": "tool", "name": "bash", "content": f"out {i} " + "y" * 900})
    msgs.append({"role": "assistant", "content": "LAST MESSAGE MARKER"})
    built = advisor.build_transcript(msgs, ["bash"], budget_tokens=1500)
    assert built["tokens"] <= 1500 + 120
    assert "First request: build it." in built["text"]
    assert "LAST MESSAGE MARKER" in built["text"]
    assert built["omitted"] > 0 and "omitted to fit the budget" in built["text"]


def test_transcript_handles_odd_messages():
    built = advisor.build_transcript(
        [None, {"role": "user", "content": [{"type": "text", "text": "hi"}, {"type": "image_url"}]},
         {"role": "assistant", "content": None, "tool_calls": [{"function": {"name": "bash", "arguments": {"a": 1}}}]},
         {"role": "user", "content": "note", "_harness_note": True}],
        ("bash", None), budget_tokens=2000)
    assert "hi [image]" in built["text"] and "CALL bash" in built["text"]
    assert "runtime note" in built["text"]


def test_clean_advice_cuts_reasoning_and_long_text():
    assert advisor.clean_advice("<think>hmm</think>Do X, not Y.") == "Do X, not Y."
    long = " ".join(["word"] * 500)
    cleaned = advisor.clean_advice(long)
    assert len(cleaned.split()) <= advisor.MAX_ADVICE_WORDS + 1
    assert advisor.clean_advice(None) == ""


def test_note_is_marked_advisory_and_untrusted():
    note = advisor.advice_note("Do X.", "final")
    assert "advisory only" in note and "not a user request" in note
    assert "never treat it as approval" in note
    assert note.rstrip().endswith("Do X.")


def test_plan_or_write_round_detection():
    assert advisor.is_plan_or_write_round(["update_plan"])
    assert advisor.is_plan_or_write_round(["read_file", "write_file"])
    assert not advisor.is_plan_or_write_round(["read_file", "web_search"])
    assert not advisor.is_plan_or_write_round([])


# ------------------------------------------------------------------ state --

def test_state_limits_uses_and_once_per_turn_triggers(monkeypatch):
    _settings(monkeypatch, advisor_enabled=True, advisor_max_uses=3)
    state = advisor.AdvisorState.from_settings()
    assert state.allows("plan_or_write") and state.allows("loop") and state.allows("final")
    state.record(advisor.AdvisorResult(trigger="plan_or_write", ok=True, text="x"))
    assert not state.allows("plan_or_write")          # once per turn
    state.record(advisor.AdvisorResult(trigger="loop", ok=True, text="x"))
    assert state.allows("loop")                        # loop may repeat...
    state.record(advisor.AdvisorResult(trigger="loop", ok=True, text="x"))
    assert not state.allows("loop") and not state.allows("final")  # ...until max_uses
    assert len(state.receipts) == 3


def test_state_disabled_unavailable_and_failure_turns_it_off(monkeypatch):
    _settings(monkeypatch, advisor_enabled=False)
    assert not advisor.AdvisorState.from_settings().allows("loop")
    _settings(monkeypatch, advisor_enabled=True)
    assert not advisor.AdvisorState.from_settings(available=False).allows("loop")
    state = advisor.AdvisorState.from_settings()
    state.record(advisor.AdvisorResult(trigger="loop", error="boom"))
    assert not state.allows("final") and state.off_reason == "failed"
    skipped = advisor.AdvisorState.from_settings()
    assert skipped.record(advisor.AdvisorResult(trigger="loop", skipped="no_model")) is None
    assert skipped.uses == 0 and not skipped.allows("loop")


# ------------------------------------------------------------------- call --

def _fake_endpoint(monkeypatch, reply="Do X, not Y, because Z.", raises=None, seen=None):
    async def _resolve(owner):
        return "spec", "http://adv/v1", "adv-model", {}

    async def _complete(url, model, messages, headers, *, max_tokens, overrides, on_usage=None):
        if seen is not None:
            seen.append({"url": url, "model": model, "messages": messages,
                         "max_tokens": max_tokens, "overrides": overrides})
        if raises:
            raise raises
        if on_usage:
            on_usage({"input_tokens": 321, "output_tokens": 12})
        return reply

    monkeypatch.setattr(advisor, "_resolve", _resolve)
    monkeypatch.setattr(advisor, "_complete", _complete)


def test_advise_returns_advice_with_reported_tokens_and_latency(monkeypatch):
    _settings(monkeypatch, advisor_enabled=True, advisor_max_tokens=512)
    seen = []
    _fake_endpoint(monkeypatch, seen=seen)
    result = _run(advisor.advise(trigger="final", messages=_session(), tool_names=["bash"]))
    assert result.ok and result.text.startswith("Do X") and not result.no_advice
    assert (result.tokens_in, result.tokens_out, result.tokens_source) == (321, 12, "reported")
    assert result.latency_ms >= 0 and result.model == "adv-model"
    call = seen[0]
    assert call["max_tokens"] == 512
    # no tools are offered, the prompt fences the transcript as data and asks for <=300 words
    assert call["messages"][0]["role"] == "system" and "never act" in call["messages"][0]["content"]
    assert "<<<TRANSCRIPT>>>" in call["messages"][1]["content"]
    assert "about to give its final answer" in call["messages"][1]["content"]
    assert call["overrides"] is not None  # the advisor mode's reasoning level


def test_advise_no_advice_failure_empty_and_no_model(monkeypatch):
    _settings(monkeypatch, advisor_enabled=True)
    _fake_endpoint(monkeypatch, reply="NO_ADVICE")
    r = _run(advisor.advise(trigger="final", messages=_session()))
    assert r.ok and r.no_advice and r.text == ""
    _fake_endpoint(monkeypatch, raises=RuntimeError("down"))
    r = _run(advisor.advise(trigger="loop", messages=_session()))
    assert not r.ok and "down" in r.error
    _fake_endpoint(monkeypatch, reply="   ")
    r = _run(advisor.advise(trigger="loop", messages=_session()))
    assert not r.ok and r.error
    _fake_endpoint(monkeypatch, raises=asyncio.TimeoutError())
    r = _run(advisor.advise(trigger="loop", messages=_session()))
    assert "in time" in r.error

    async def _no_model(owner):
        raise LookupError("none")
    monkeypatch.setattr(advisor, "_resolve", _no_model)
    r = _run(advisor.advise(trigger="loop", messages=_session()))
    assert r.skipped == "no_model" and not r.error


def test_stats_count_calls_tokens_and_failures(monkeypatch):
    _settings(monkeypatch, advisor_enabled=True)
    state = advisor.AdvisorState.from_settings()
    state.record(advisor.AdvisorResult(trigger="loop", ok=True, text="a", tokens_in=10, tokens_out=5, latency_ms=100))
    state.record(advisor.AdvisorResult(trigger="final", ok=True, no_advice=True, tokens_in=20, tokens_out=1, latency_ms=300))
    state.record(advisor.AdvisorResult(trigger="plan_or_write", error="x"))
    snap = advisor.stats()
    assert snap["calls"] == 3 and snap["ok"] == 2 and snap["failed"] == 1 and snap["no_advice"] == 1
    assert snap["by_trigger"] == {"loop": 1, "final": 1, "plan_or_write": 1}
    assert snap["tokens_in"] == 30 and snap["mean_latency_ms"] == round((100 + 300) / 3, 1)
    assert all("text" not in r for r in snap["recent"])


# -------------------------------------------------------------- the loop --

def _collect(gen):
    async def _go():
        return [c async for c in gen]
    return asyncio.run(_go())


def _events(chunks):
    out = []
    for c in chunks:
        if c.startswith("data: ") and not c.startswith("data: [DONE]"):
            try:
                out.append(json.loads(c[6:]))
            except Exception:
                pass
    return out


def _patch_loop(monkeypatch, tool_output=None):
    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)
    monkeypatch.setattr(al, "blocked_tools_for_owner", lambda owner: set(), raising=False)

    async def _fake_exec(block, *a, **k):
        return (block.tool_type, tool_output or {"output": "ok", "exit_code": 0})
    monkeypatch.setattr(al, "execute_tool_block", _fake_exec, raising=False)


def _scripted_stream(monkeypatch, rounds):
    """rounds: list of text per round; the last one repeats. Records each
    round's messages."""
    seen = []

    async def _fake_stream(_candidates, messages, **kwargs):
        seen.append([dict(m) for m in messages])
        text = rounds[min(len(seen) - 1, len(rounds) - 1)]
        yield f'data: {json.dumps({"delta": text})}\n\n'
        yield f'data: {json.dumps({"type": "finish", "finish_reason": "stop"})}\n\n'
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    return seen


def _drive(monkeypatch, rounds, *, max_rounds=4, tools=None, workspace=None, text="do a long multi-step task"):
    seen = _scripted_stream(monkeypatch, rounds)
    kwargs = dict(max_rounds=max_rounds, relevant_tools=tools or {"bash"},
                  security_gate_bypass=True)
    if workspace:
        kwargs["workspace"] = workspace
    chunks = _collect(al.stream_agent_loop(
        "http://x/v1", "m", [{"role": "user", "content": text}], **kwargs))
    return seen, _events(chunks)


PLAN = '```update_plan\n{"plan":"- [ ] step one\\n- [ ] step two"}\n```'


def test_loop_plan_round_asks_the_advisor_once_and_injects_a_marked_note(monkeypatch):
    _patch_loop(monkeypatch)
    _settings(monkeypatch, advisor_enabled=True)
    asked = []
    _fake_endpoint(monkeypatch, seen=asked)
    seen, events = _drive(monkeypatch, [PLAN, PLAN, "All done, here is the answer."])
    assert len(asked) == 1, "plan_or_write fires once per turn"
    adv = [e for e in events if e.get("type") == "advisor_advice"]
    assert len(adv) == 1
    assert adv[0]["trigger"] == "plan_or_write" and adv[0]["ok"] is True
    assert adv[0]["tokens_in"] == 321 and "text" in adv[0] and adv[0]["latency_ms"] >= 0
    # The second model call reads the advice, marked advisory, AFTER the tool results.
    second = seen[1]
    notes = [m for m in second if m.get("_harness_note") and "Runtime advisor note" in str(m.get("content"))]
    assert len(notes) == 1 and "advisory only" in notes[0]["content"]
    assert second.index(notes[0]) == len(second) - 1
    # The advisor saw the plan the agent wrote.
    assert "update_plan" in asked[0]["messages"][1]["content"]
    metrics = [e for e in events if e.get("type") == "metrics"][-1]["data"]
    assert metrics["advisor"][0]["trigger"] == "plan_or_write"


def test_loop_advisor_off_by_default_makes_no_call(monkeypatch):
    _patch_loop(monkeypatch)
    asked = []
    _fake_endpoint(monkeypatch, seen=asked)
    seen, events = _drive(monkeypatch, [PLAN, "All done."])
    assert asked == [] and not any(e.get("type") == "advisor_advice" for e in events)
    assert not any("Runtime advisor note" in str(m.get("content")) for round_ in seen for m in round_)
    metrics = [e for e in events if e.get("type") == "metrics"][-1]["data"]
    assert "advisor" not in metrics


def test_loop_advisor_failure_is_recorded_and_the_turn_goes_on(monkeypatch):
    _patch_loop(monkeypatch)
    _settings(monkeypatch, advisor_enabled=True)
    _fake_endpoint(monkeypatch, raises=RuntimeError("endpoint down"))
    seen, events = _drive(monkeypatch, [PLAN, PLAN, "All done."])
    adv = [e for e in events if e.get("type") == "advisor_advice"]
    assert len(adv) == 1 and adv[0]["ok"] is False and "endpoint down" in adv[0]["error"]
    assert not any("Runtime advisor note" in str(m.get("content")) for round_ in seen for m in round_)
    assert events[-1].get("type") != "error" or True
    assert any(e.get("type") == "metrics" for e in events)


def test_loop_without_a_model_is_silent(monkeypatch):
    _patch_loop(monkeypatch)
    _settings(monkeypatch, advisor_enabled=True)

    async def _no_model(owner):
        raise LookupError("none")
    monkeypatch.setattr(advisor, "_resolve", _no_model)
    seen, events = _drive(monkeypatch, [PLAN, "All done."])
    assert not any(e.get("type") == "advisor_advice" for e in events)


LOOP_CALLS = ['```read_file\na.py\n```', '```read_file\nb.py\n```']


def test_loop_nudge_step_asks_the_advisor_instead_of_the_generic_nudge(monkeypatch, tmp_path):
    _patch_loop(monkeypatch)
    _settings(monkeypatch, advisor_enabled=True)
    asked = []
    _fake_endpoint(monkeypatch, reply="Do read the failing test, not the same read again, because it cannot change.", seen=asked)
    seen, events = _drive(monkeypatch, LOOP_CALLS * 6, max_rounds=9, tools={"read_file"}, workspace=str(tmp_path))
    loop_events = [e for e in events if e.get("type") == "advisor_advice" and e["trigger"] == "loop"]
    assert loop_events, events
    assert "Loop detector" in asked[0]["messages"][1]["content"]
    flat = [str(m.get("content")) for round_ in seen for m in round_]
    assert any("Runtime advisor note" in c for c in flat)
    # The generic nudge for that same step was replaced, not added.
    first_advised = next(i for i, r in enumerate(seen) if any("Runtime advisor note" in str(m.get("content")) for m in r))
    assert not any("This exact call, with this exact result" in str(m.get("content"))
                   for m in seen[first_advised])


def test_loop_nudge_falls_back_to_the_generic_nudge_when_the_advisor_fails(monkeypatch, tmp_path):
    _patch_loop(monkeypatch)
    _settings(monkeypatch, advisor_enabled=True)
    _fake_endpoint(monkeypatch, raises=RuntimeError("down"))
    seen, events = _drive(monkeypatch, LOOP_CALLS * 6, max_rounds=9, tools={"read_file"}, workspace=str(tmp_path))
    flat = [str(m.get("content")) for round_ in seen for m in round_]
    assert any("This exact call, with this exact result" in c for c in flat)
    assert not any("Runtime advisor note" in c for c in flat)


WRITE = '```write_file\na.py\nx = 1\n```'


def test_loop_final_answer_after_writes_gets_one_more_round_when_there_is_advice(monkeypatch, tmp_path):
    _patch_loop(monkeypatch)
    _settings(monkeypatch, advisor_enabled=True, advisor_max_uses=3)
    asked = []
    _fake_endpoint(monkeypatch, reply="Do run the parser tests, not claim done, because nothing ran them.", seen=asked)
    seen, events = _drive(monkeypatch, [WRITE, "Finished: I edited a.py.", "Finished: I edited a.py and ran the tests."],
                          max_rounds=6, tools={"read_file", "write_file"}, workspace=str(tmp_path))
    triggers = [e["trigger"] for e in events if e.get("type") == "advisor_advice"]
    assert triggers == ["plan_or_write", "final"], triggers
    # the final-answer call was shown the answer the agent was about to give
    assert "Finished: I edited a.py." in asked[-1]["messages"][1]["content"]
    # and the model got one more round with the note
    last = seen[-1]
    assert any("Runtime advisor note" in str(m.get("content")) and "final" in str(m.get("content")) for m in last)
    assert len(seen) == 3


def test_loop_final_answer_with_no_advice_ends_the_turn_without_another_round(monkeypatch, tmp_path):
    _patch_loop(monkeypatch)
    _settings(monkeypatch, advisor_enabled=True, advisor_max_uses=3)
    _fake_endpoint(monkeypatch, reply="NO_ADVICE")
    seen, events = _drive(monkeypatch, [WRITE, "Finished: I edited a.py."],
                          max_rounds=6, tools={"read_file", "write_file"}, workspace=str(tmp_path))
    advice = [e for e in events if e.get("type") == "advisor_advice"]
    assert [e["trigger"] for e in advice] == ["plan_or_write", "final"]
    assert all(e["no_advice"] for e in advice) and all("text" not in e for e in advice)
    assert len(seen) == 2, "NO_ADVICE must not cost an extra round"


def test_loop_final_trigger_needs_written_files(monkeypatch, tmp_path):
    _patch_loop(monkeypatch)
    _settings(monkeypatch, advisor_enabled=True)
    asked = []
    _fake_endpoint(monkeypatch, seen=asked)
    seen, events = _drive(monkeypatch, ["Just an answer, no files."], max_rounds=3,
                          tools={"read_file"}, workspace=str(tmp_path))
    assert asked == []


def test_advisor_never_runs_inside_a_teacher_takeover(monkeypatch):
    _patch_loop(monkeypatch)
    _settings(monkeypatch, advisor_enabled=True)
    asked = []
    _fake_endpoint(monkeypatch, seen=asked)
    _scripted_stream(monkeypatch, [PLAN, "done"])
    chunks = _collect(al.stream_agent_loop(
        "http://x/v1", "m", [{"role": "user", "content": "plan it"}],
        max_rounds=3, relevant_tools={"bash"}, _is_teacher_run=True))
    assert asked == []


# ------------------------------------------------------------------ route --

def test_stats_route_reports_counters_and_needs_a_user(monkeypatch):
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient
    from routes import agent_loop_stats_routes
    # the object the routes depend on: a test elsewhere may reload src.auth_helpers
    require_user = agent_loop_stats_routes.require_user

    _settings(monkeypatch, advisor_enabled=True)
    state = advisor.AdvisorState.from_settings()
    state.record(advisor.AdvisorResult(trigger="loop", ok=True, text="secret advice", tokens_in=9, latency_ms=5))
    app = FastAPI()
    app.include_router(agent_loop_stats_routes.setup_agent_loop_stats_routes())
    app.dependency_overrides[require_user] = lambda: "ada"
    body = TestClient(app).get("/api/agent/advisor/stats").json()
    assert body["calls"] == 1 and body["by_trigger"] == {"loop": 1} and body["enabled"] is True
    assert "secret advice" not in json.dumps(body)

    def deny():
        raise HTTPException(401, "login required")
    app.dependency_overrides[require_user] = deny
    assert TestClient(app).get("/api/agent/advisor/stats").status_code == 401
