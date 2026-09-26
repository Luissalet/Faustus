"""Deep Research thinks at its own level (src/mode_effort.py): the reasoning
stages ask for it, reading a page and one-word checks do not."""
import asyncio

import src.llm_core as llm_core
from src import mode_effort
from src.deep_research import DeepResearcher


def _researcher(monkeypatch, effort=None, settings=None):
    settings = settings or {}
    monkeypatch.setattr("src.settings.get_setting", lambda k, d=None: settings.get(k, d))
    return DeepResearcher("https://api.example.test/v1", "some-model", effort=effort)


def _capture(monkeypatch):
    seen = []

    async def fake_call(**kw):
        seen.append(kw)
        return "ok"
    monkeypatch.setattr(llm_core, "llm_call_async", fake_call)
    return seen


def test_default_is_max_for_reasoning_and_off_for_reading(monkeypatch):
    r = _researcher(monkeypatch)
    seen = _capture(monkeypatch)
    asyncio.run(r._llm([{"role": "user", "content": "plan"}], max_tokens=1024))
    asyncio.run(r._llm([{"role": "user", "content": "page"}], max_tokens=2048, stage="read"))
    asyncio.run(r._llm([{"role": "user", "content": "cat?"}], max_tokens=20, stage="quick"))
    think, read, quick = (s["gen_overrides"] for s in seen)
    assert think["think"] is True and think["reasoning_effort"] == "max" and think["reasoning_budget"] == 16384
    assert read == {"think": False}
    assert quick is None


def test_a_picked_level_and_a_saved_one(monkeypatch):
    r = _researcher(monkeypatch, effort="medium")
    assert r.effort_level == "medium"
    r2 = _researcher(monkeypatch, settings={"mode_effort_research": "high"})
    assert r2.effort_level == "high"
    assert mode_effort.level_for("research", "auto") == "high"  # the saved setting
    assert mode_effort.MODE_DEFAULTS["research"] == "max"


def test_levels():
    assert mode_effort.overrides_for_level("off") == {"think": False}
    assert mode_effort.overrides_for_level("auto") is None
    assert mode_effort.normalize("xhigh") == "max" and mode_effort.normalize("rubbish") == "auto"


def test_delegate_effort_has_a_max_level():
    from src.effort_profile import resolve
    out = resolve("max")
    assert out["gen_overrides"] == {"think": True, "reasoning_effort": "max", "reasoning_budget": 16384}
    assert out["hint"]
