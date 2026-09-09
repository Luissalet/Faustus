"""A run that gathered nothing is a failure with a cause, not a report.

08-09-2026, 22:13: qwen3.8:27b-q8_0, spilling to RAM, timed out on every
call. Planning fell back to the raw question, the 300s budget ran out before
a single round could start, and the handler logged "IterResearch completed
successfully — Rounds: 1, Queries: 0, URLs: 0" and saved "No information
could be gathered" as the report. The person read that as "the search is
broken". The search never ran.
"""
import asyncio

import pytest

from src.deep_research import DeepResearcher, ResearchFailed


def _researcher(**kw):
    return DeepResearcher(llm_endpoint="http://127.0.0.1:11434/v1/chat/completions",
                          llm_model="slow", max_time=kw.pop("max_time", 300), **kw)


def test_a_model_that_never_answers_is_named_as_the_cause(monkeypatch):
    r = _researcher(max_rounds=3)

    async def _timeout(*a, **k):
        raise TimeoutError("POST /api/chat timed out after 3 attempts")
    monkeypatch.setattr(r, "_llm", _timeout)
    # The clock is what ends this run, as it did that night.
    ticks = iter([False, True])
    monkeypatch.setattr(r, "_time_exceeded", lambda: next(ticks, True))

    with pytest.raises(ResearchFailed) as err:
        asyncio.run(r.research("Whiplash guideline for physiotherapists"))

    msg = str(err.value)
    assert "never answered in time" in msg
    assert "timed out" in msg
    assert "smaller quantization" in msg          # what to do about it
    assert err.value.causes and err.value.causes[0].startswith("planning:")
    assert r._rounds_started == 0


def test_search_that_finds_nothing_is_named_too(monkeypatch):
    r = _researcher(max_rounds=4, max_empty_rounds=2, min_rounds=3)

    calls = {"n": 0}

    async def _llm(messages, **k):
        calls["n"] += 1                     # fresh queries each round, or dedupe ends it early
        return f'["query {calls["n"]}a", "query {calls["n"]}b"]'
    monkeypatch.setattr(r, "_llm", _llm)
    monkeypatch.setattr(r, "_create_plan", lambda q: _ready("Sub-questions: x"))
    monkeypatch.setattr(r, "_classify_category", lambda q: _ready(None))

    async def _nothing(queries, question):
        r._last_search_error = "searxng: no results from search provider(s)"
        return []
    monkeypatch.setattr(r, "_search_and_extract", _nothing)

    with pytest.raises(ResearchFailed) as err:
        asyncio.run(r.research("anything"))
    assert "returned nothing in 2 rounds" in str(err.value)
    assert "searxng" in str(err.value)


def _ready(value):
    async def _coro(*a, **k):
        return value
    return _coro()


@pytest.mark.asyncio
async def test_the_handler_reports_it_as_an_error_not_a_report(monkeypatch):
    from src.research_handler import ResearchHandler
    handler = ResearchHandler()

    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(handler, "_probe_endpoint", _noop)
    monkeypatch.setattr(handler, "_ensure_search_backend", _noop)

    class _Empty:
        evolving_report = ""
        findings = []
        def __init__(self, **kw): pass
        async def research(self, *a, **k):
            raise ResearchFailed("The model never answered in time: 3 call(s) timed out",
                                 causes=["planning: TimeoutError"])
        def get_stats(self): return {}
    monkeypatch.setattr("src.deep_research.DeepResearcher", _Empty)

    handler.start_research("rp-empty-run", "q", "http://127.0.0.1:11434/v1/chat/completions", "slow")
    entry = handler._active_tasks["rp-empty-run"]
    await entry["task"]

    assert entry["status"] == "error"
    assert entry["result"] is None                       # no fake report
    assert "never answered in time" in entry["error"]     # the cause, kept
    assert entry["progress"]["phase"] == "error"
