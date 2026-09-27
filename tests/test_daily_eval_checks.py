"""The everyday battery's checks are deterministic (scripts/daily_eval.py)."""
import importlib.util
import io
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("daily_eval", ROOT / "scripts" / "daily_eval.py")
daily_eval = importlib.util.module_from_spec(spec)
spec.loader.exec_module(daily_eval)


def _failed(task, result):
    return [c["check"] for c in daily_eval.check(task, result) if not c["ok"]]


def test_a_right_answer_passes_and_a_wrong_one_does_not():
    task = {"answer_all": [r"\b45\b"], "tools_none": ["web_search"], "max_cards": 0}
    assert _failed(task, {"answer": "Hay 45 días.", "tools": [], "cards": 0}) == []
    assert _failed(task, {"answer": "Hay 44 días.", "tools": ["web_search"], "cards": 1}) == [
        r"answer ~ \b45\b", "did not use web_search", "at most 0 approval cards"]


def test_live_eval_requires_the_model_that_actually_served_the_turn():
    selected = "qwen3.8-27b-q8-llamacpp"
    result = {"answer": "24,84 €", "tools": [], "expected_model": selected,
              "observed_models": [selected], "fallbacks": []}
    assert _failed({}, result) == []
    assert "served by selected model" in _failed({}, {**result, "observed_models": ["qwen2.5-3b-helper"]})
    assert "served by selected model" in _failed({}, {**result, "observed_models": []})
    assert "no model fallback" in _failed({}, {**result, "fallbacks": [
        {"selected_model": selected, "answered_by": "qwen2.5-3b-helper"}]})


def test_multiturn_report_keeps_first_turn_cost_and_model_failure():
    combined = daily_eval.combine_turns([
        {"answer": "45 días", "seconds": 91.2, "observed_models": ["qwen2.5-3b-helper"],
         "fallbacks": [{"answered_by": "qwen2.5-3b-helper"}], "error": ""},
        {"answer": "6 semanas", "seconds": 15.5,
         "observed_models": ["qwen3.8-27b-q8-llamacpp"], "fallbacks": [], "error": ""},
    ])
    assert combined["answer"] == "6 semanas"
    assert combined["seconds"] == 106.7
    assert len(combined["turns"]) == 2
    assert "served by selected model" in _failed({}, {**combined,
        "expected_model": "qwen3.8-27b-q8-llamacpp"})
    assert "no model fallback" in _failed({}, {**combined,
        "expected_model": "qwen3.8-27b-q8-llamacpp"})


def test_approval_pause_does_not_leak_into_final_eval_answer():
    class FakeClient(daily_eval.Client):
        def __init__(self):
            self.calls = 0

        def form(self, path, data, timeout=30):
            self.calls += 1
            events = ([{"type": "model_info", "model": "main"},
                       {"type": "delta", "delta": "Allow this task to continue?"},
                       {"type": "ask_user", "data": {"kind": "tool_approval", "approval_id": "a1"}}]
                      if self.calls == 1 else
                      [{"type": "model_info", "model": "main"},
                       {"type": "delta", "delta": "Hecho."}])
            return io.BytesIO("".join(f"data: {json.dumps(ev)}\n\n" for ev in events).encode())

    result = FakeClient().turn("session", "mark favorite", "agent", "main", False, 30, True)
    assert result["answer"] == "Hecho."
    assert result["observed_models"] == ["main", "main"]


def test_raw_harness_text_fails_any_task():
    assert _failed({}, {"answer": "AI: No events between x\nok", "tools": []}) == ["no raw text ^AI: "]
    assert _failed({}, {"answer": "Allow this task to continue?\nListo", "tools": []})


def test_first_tool_and_mcp_names():
    task = {"first_tool_any": ["manage_calendar"], "tools_any": ["calc"]}
    result = {"answer": "libre", "tools": ["manage_calendar", "mcp__26a426d3__calc"], "cards": 0}
    assert _failed(task, result) == []
    assert sorted(_failed(task, {"answer": "x", "tools": ["lookup_tools", "manage_calendar"]})) == [
        "first tool in manage_calendar", "used one of calc"]


def test_the_task_file_is_valid():
    tasks = json.loads((ROOT / "scripts" / "daily_eval_tasks.json").read_text(encoding="utf-8"))
    assert len({t["id"] for t in tasks}) == len(tasks) >= 15
    for t in tasks:
        assert t["messages"] and all(isinstance(m, str) and m for m in t["messages"])


def test_set_pairs_become_a_typed_settings_patch():
    de = daily_eval
    patch = de.parse_overrides(["agent_followup_reasoning_budget=1536", "x=true", "name=plain text", 'q="1"'])
    assert patch == {"agent_followup_reasoning_budget": 1536, "x": True, "name": "plain text", "q": "1"}
    import pytest
    with pytest.raises(ValueError):
        de.parse_overrides(["no-equals-sign"])


def test_an_ab_arm_restores_the_previous_settings_even_when_the_run_fails(monkeypatch):
    from types import SimpleNamespace
    de = daily_eval
    saved = []

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def settings(self):
            return {"agent_followup_reasoning_budget": 0, "other": 1}

        def save_settings(self, patch):
            saved.append(dict(patch))

    monkeypatch.setattr(de, "Client", FakeClient)

    def boom(args, client):
        raise RuntimeError("engine down")
    monkeypatch.setattr(de, "_run", boom)
    args = SimpleNamespace(base="http://x", set=["agent_followup_reasoning_budget=2048"])
    import pytest
    with pytest.raises(RuntimeError):
        de.run(args)
    assert saved == [{"agent_followup_reasoning_budget": 2048}, {"agent_followup_reasoning_budget": 0}]


def test_cache_totals_sum_the_tasks_and_give_the_reuse_share():
    cache_totals, markdown = daily_eval.cache_totals, daily_eval.markdown
    rows = [{"prompt_cache": {"rounds": 3, "processed": 2000, "cached": 18000, "lost_rounds": 1}},
            {"prompt_cache": {"rounds": 1, "processed": 1000, "cached": 0, "lost_rounds": 0}},
            {}]
    total = cache_totals(rows)
    assert total == {"rounds": 4, "processed": 3000, "cached": 18000, "lost_rounds": 1, "reuse": 0.857}
    report = {"when": "x", "passed": 0, "total": 0, "seconds": 0, "model": "m", "base": "b",
              "prompt_cache": total, "tasks": []}
    assert "86% reutilizado, 1 rondas con caché perdida" in markdown(report)
    assert cache_totals([{}])["reuse"] is None
