"""A Deep Research call that spends its budget thinking and writes nothing is redone once without thinking,
and the run stops asking for that effort."""
import asyncio

from src.deep_research import DeepResearcher


def _researcher():
    r = DeepResearcher(llm_endpoint="https://api.example.com/v1", llm_model="m", max_report_tokens=1024)
    r._think_overrides = {"think": True, "reasoning_budget": 4096}
    r._read_overrides = {"think": True, "reasoning_budget": 1024}
    return r


def test_empty_after_thinking_is_redone_without_thinking(monkeypatch):
    calls = []

    async def fake(**kw):
        calls.append(kw.get("gen_overrides"))
        return "" if len(calls) == 1 else "the answer"

    monkeypatch.setattr("src.llm_core.llm_call_async", fake)
    r = _researcher()
    out = asyncio.run(r._llm([{"role": "user", "content": "q"}]))
    assert out == "the answer"
    assert calls[0]["think"] is True and calls[1]["think"] is False
    assert "reasoning_budget" not in calls[1]
    assert r.thinking_fallbacks == 1
    # the next calls of the run do not ask for the same effort again
    assert r._think_overrides["think"] is False and r._read_overrides["think"] is False
    assert "Thinking skipped" in r.get_stats()


def test_a_normal_answer_is_left_alone(monkeypatch):
    calls = []

    async def fake(**kw):
        calls.append(1)
        return "fine"

    monkeypatch.setattr("src.llm_core.llm_call_async", fake)
    r = _researcher()
    assert asyncio.run(r._llm([{"role": "user", "content": "q"}])) == "fine"
    assert len(calls) == 1 and not getattr(r, "thinking_fallbacks", 0)
    assert "Thinking skipped" not in r.get_stats()


def test_quick_stage_never_retries(monkeypatch):
    calls = []

    async def fake(**kw):
        calls.append(1)
        return ""

    monkeypatch.setattr("src.llm_core.llm_call_async", fake)
    r = _researcher()
    assert asyncio.run(r._llm([{"role": "user", "content": "q"}], stage="quick")) == ""
    assert len(calls) == 1
