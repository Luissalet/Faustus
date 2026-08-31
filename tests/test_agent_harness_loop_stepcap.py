"""Step-limit auto-continue: the harness grants one extra cycle before the
Continue button (agent_auto_continue_cycles default 1)."""

from tests.test_agent_harness_loop import _patch_common, _scripted_stream, _events, _collect
import src.agent_loop as al


def test_step_limit_auto_continues_once_then_offers_continue(tmp_path, monkeypatch):
    _patch_common(monkeypatch)
    calls = _scripted_stream(monkeypatch, [('```update_plan\n{"plan":"- [ ] keep going"}\n```', "tool_calls")])
    gen = al.stream_agent_loop(
        "http://127.0.0.1:11434/v1", "qwen3-coder:30b",
        [{"role": "user", "content": "do a long multi-step task in the repo"}],
        max_rounds=2, relevant_tools={"update_plan"}, workspace=str(tmp_path),
    )
    events = _events(_collect(gen))
    autos = [e for e in events if e.get("type") == "harness_check" and e.get("status") == "auto_continue"]
    assert len(autos) == 1 and autos[0]["reason"] == "rounds" and autos[0]["round"] == 2, events
    exhausted = [e for e in events if e.get("type") == "rounds_exhausted"]
    assert exhausted and exhausted[0]["rounds"] == 4
    assert calls["n"] == 4
