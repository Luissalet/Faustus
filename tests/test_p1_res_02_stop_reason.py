"""RES-02 — search iterativa con parada razonada.

`DeepResearcher` always had several real stopping paths but never recorded
WHY it stopped, and a round that repeated a query without new evidence never
changed strategy on the next round -- both added by this lote.
"""
import asyncio

import pytest

from src.deep_research import (
    STOP_REASON_LLM_COVERAGE,
    STOP_REASON_MAX_ROUNDS,
    DeepResearcher,
    classify_stop_reason,
    stop_rationale_text,
)


def _researcher(**kw):
    return DeepResearcher(llm_endpoint="http://127.0.0.1:11434/v1/chat/completions",
                          llm_model="m", max_time=300, **kw)


def _ready(value):
    async def _coro(*a, **k):
        return value
    return _coro()


# ---------------------------------------------------------------------------
# classify_stop_reason / stop_rationale_text: pure functions
# ---------------------------------------------------------------------------

def test_classify_stop_reason_names_a_stable_code_and_a_sentence():
    info = classify_stop_reason(STOP_REASON_MAX_ROUNDS, round_num=8, max_rounds=8, urls_fetched=12, findings=9)
    assert info["code"] == STOP_REASON_MAX_ROUNDS
    assert "maximum number of rounds" in info["reason"]
    assert info["rounds_completed"] == "8 of 8"
    assert info["sources_gathered"] == 12
    assert info["findings_gathered"] == 9


def test_stop_rationale_text_is_readable_and_handles_none():
    info = classify_stop_reason(STOP_REASON_LLM_COVERAGE, round_num=3, max_rounds=8, urls_fetched=5, findings=4)
    text = stop_rationale_text(info)
    assert "3 of 8" in text
    assert "coverage" in text.lower()
    assert stop_rationale_text(None) == ""


# ---------------------------------------------------------------------------
# Integration: a real (monkeypatched) run records WHY it stopped.
# ---------------------------------------------------------------------------

def _wire_common(r, monkeypatch, pages):
    async def _search(query):
        return list(pages)
    monkeypatch.setattr(r, "_search", _search)

    async def _extract(url, question, title=""):
        return {"url": url, "title": title, "content": f"facts from {url}", "relevance": 0.9}
    monkeypatch.setattr(r, "_fetch_and_extract", _extract)
    monkeypatch.setattr(r, "_create_plan", lambda q: _ready("plan"))
    monkeypatch.setattr(r, "_classify_category", lambda q: _ready(None))
    monkeypatch.setattr(r, "_synthesize", lambda *a, **k: _ready("report so far"))
    monkeypatch.setattr(r, "_final_report", lambda q, rep: _ready("final: " + rep))


def test_regression_stop_reason_is_recorded_when_llm_decides_to_stop(monkeypatch):
    """Without `self.stop_reason` being set, a caller has no way to tell a
    coverage-judged stop from any other -- this is what RES-02's own
    "the report says why it stopped" requires."""
    r = _researcher(max_rounds=5, min_rounds=1)
    pages = [{"url": f"https://example.org/{i}", "title": "T"} for i in range(3)]
    _wire_common(r, monkeypatch, pages)
    calls = {"n": 0}

    async def _llm(messages, **k):
        calls["n"] += 1
        text = messages[-1]["content"]
        if "YES" in text and "NO" in text:
            return "YES"  # stop after round 1
        return f'["query {calls["n"]}"]'
    monkeypatch.setattr(r, "_llm", _llm)

    asyncio.run(r.research("test question"))
    assert r.stop_reason is not None
    assert r.stop_reason["code"] == STOP_REASON_LLM_COVERAGE
    assert r.stop_reason["rounds_completed"].startswith("1 of")


def test_stop_reason_is_max_rounds_when_the_model_never_says_stop(monkeypatch):
    r = _researcher(max_rounds=2, min_rounds=1)
    pages = [{"url": f"https://example.org/{i}", "title": "T"} for i in range(3)]
    _wire_common(r, monkeypatch, pages)
    calls = {"n": 0}

    async def _llm(messages, **k):
        calls["n"] += 1
        text = messages[-1]["content"]
        if "YES" in text and "NO" in text:
            return "NO"
        return f'["query {calls["n"]}"]'
    monkeypatch.setattr(r, "_llm", _llm)

    asyncio.run(r.research("test question"))
    assert r.stop_reason is not None
    assert r.stop_reason["code"] == STOP_REASON_MAX_ROUNDS
    assert r.stop_reason["rounds_completed"] == "2 of 2"


# ---------------------------------------------------------------------------
# Query strategy changes after a stalled (no-new-evidence) round.
# ---------------------------------------------------------------------------

def test_regression_repeated_empty_round_changes_query_strategy(monkeypatch):
    """RES-02 acceptance: repeating a query without new evidence must trigger
    a strategy change. Without `self._consecutive_empty_rounds` feeding
    `_generate_queries`, round 2's prompt after an empty round 1 is
    identical to any other follow-up round's."""
    r = _researcher()
    seen = {}

    async def _llm(messages, **k):
        seen["prompt"] = messages[-1]["content"]
        return '["q"]'
    monkeypatch.setattr(r, "_llm", _llm)

    r._consecutive_empty_rounds = 0
    asyncio.run(r._generate_queries("q?", "some report", 2))
    assert "change strategy" not in seen["prompt"].lower()

    r._consecutive_empty_rounds = 1
    asyncio.run(r._generate_queries("q?", "some report", 3))
    assert "change strategy" in seen["prompt"].lower()
    assert "already have partial findings" in seen["prompt"].lower()  # existing instruction preserved
