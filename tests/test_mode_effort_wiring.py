"""Council members and the teacher ask for reasoning at their mode's level,
and a council member's reasoning never becomes part of what it says."""
import asyncio
import json

from src.council.adapters import _consume_stream


def test_council_keeps_reasoning_out_of_the_answer():
    async def stream():
        yield f'data: {json.dumps({"delta": "let me think", "thinking": True})}\n\n'
        yield f'data: {json.dumps({"delta": "My view: yes."})}\n\n'
    text, _usage, error = asyncio.run(_consume_stream(stream()))
    assert text == "My view: yes." and not error


def test_teacher_call_carries_the_teacher_level(monkeypatch):
    import src.llm_core as llm_core
    from src import teacher_escalation as te
    seen = {}

    async def fake_call(url, model, messages, **kw):
        seen.update(kw)
        return "plan"
    monkeypatch.setattr(llm_core, "llm_call_async", fake_call)
    monkeypatch.setattr("src.ai_interaction._resolve_model",
                        lambda spec, owner=None: ("http://127.0.0.1:8081/v1", "m", None))
    monkeypatch.setattr("src.settings.get_setting", lambda k, d=None: d)
    out = asyncio.run(te._call_teacher("m", "help"))
    assert out == "plan"
    assert seen["gen_overrides"]["think"] is True and seen["gen_overrides"]["reasoning_effort"] == "max"
    assert seen["timeout"] == 600


def test_new_modes_defaults_and_timeouts(monkeypatch):
    from src import mode_effort
    monkeypatch.setattr("src.settings.get_setting", lambda k, d=None: d)
    assert mode_effort.level_for("consult") == "high"
    assert mode_effort.level_for("tournament") == "high"
    assert mode_effort.for_mode("bug_hunt") is None and mode_effort.for_mode("ci_analysis") is None
    ov = mode_effort.for_mode("tournament")
    assert mode_effort.timeout_for(ov, 120) == 120 + ov["reasoning_budget"] / 20.0
    assert mode_effort.timeout_for(None, 120) == 120


def test_ask_teacher_and_consult_send_their_level(monkeypatch):
    import asyncio
    from src.agent_tools import model_interaction_tools as mit
    seen = []

    async def fake_call(url, model, messages, **kw):
        seen.append(kw)
        return "answer"
    monkeypatch.setattr("src.llm_core.llm_call_async", fake_call)
    monkeypatch.setattr("src.ai_interaction._resolve_model", lambda spec, owner=None: ("http://x/v1", "m", {}))
    monkeypatch.setattr("src.settings.get_setting", lambda k, d=None: d)
    asyncio.run(mit.chat_with_model("m\nhello"))
    asyncio.run(mit.ask_teacher("m\nstuck on this"))
    assert seen[0]["gen_overrides"]["reasoning_effort"] == "high"
    assert seen[1]["gen_overrides"]["reasoning_effort"] == "max"
    assert seen[1]["timeout"] > seen[0]["timeout"] >= 120
