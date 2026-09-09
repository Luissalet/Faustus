"""An engine that keeps returning the pages already read is not an outage.

09-09-2026: through SearXNG, bing handed back the same ten URLs for every
phrasing of "whiplash associated disorders" (the film, IMDb, Mayo Clinic).
Round 2 found nothing new and the run reported "Search appears to be down".
The search was fine; it had run dry for that question. The round must say
that, and a run with findings must still write them up.
"""
import asyncio

import pytest

from src.deep_research import DeepResearcher, ResearchFailed


def _researcher(**kw):
    return DeepResearcher(llm_endpoint="http://127.0.0.1:11434/v1/chat/completions",
                          llm_model="m", max_time=300, **kw)


def _ready(value):
    async def _coro(*a, **k):
        return value
    return _coro()


def _same_pages(r, monkeypatch):
    pages = [{"url": "https://example.org/a", "title": "A"}, {"url": "https://example.org/b", "title": "B"}]

    async def _search(query):
        return list(pages)
    monkeypatch.setattr(r, "_search", _search)

    async def _extract(url, question, title=""):
        return {"url": url, "title": title, "content": f"facts from {url}", "relevance": 0.9}
    monkeypatch.setattr(r, "_fetch_and_extract", _extract)


def test_all_old_pages_is_named_not_called_an_outage(monkeypatch):
    r = _researcher(max_rounds=5, max_empty_rounds=1, min_rounds=1)
    _same_pages(r, monkeypatch)
    calls = {"n": 0}

    async def _llm(messages, **k):
        calls["n"] += 1
        text = messages[-1]["content"]
        if "YES" in text and "NO" in text:          # stop prompt: keep going
            return "NO"
        return f'["query {calls["n"]}"]'
    monkeypatch.setattr(r, "_llm", _llm)
    monkeypatch.setattr(r, "_create_plan", lambda q: _ready("plan"))
    monkeypatch.setattr(r, "_classify_category", lambda q: _ready(None))
    monkeypatch.setattr(r, "_synthesize", lambda *a, **k: _ready("report so far"))
    monkeypatch.setattr(r, "_final_report", lambda q, rep: _ready("final: " + rep))
    events = []
    r._progress = events.append

    out = asyncio.run(r.research("whiplash"))

    assert out.startswith("final:")                       # round 1's findings were written up
    assert r._last_round_note.startswith("2 result(s) came back and all 2")
    warnings = [e for e in events if e.get("phase") == "warning"]
    assert warnings and "No new pages to read" in warnings[-1]["message"]
    assert not [e for e in events if e.get("phase") == "error"]


def test_only_old_pages_and_nothing_usable_fails_with_that_cause(monkeypatch):
    r = _researcher(max_rounds=3, max_empty_rounds=1, min_rounds=1)
    _same_pages(r, monkeypatch)
    for url in ("https://example.org/a", "https://example.org/b"):
        r.urls_fetched.add(url)                            # read before this run ever started

    async def _llm(messages, **k):
        return '["q"]'
    monkeypatch.setattr(r, "_llm", _llm)
    monkeypatch.setattr(r, "_create_plan", lambda q: _ready("plan"))
    monkeypatch.setattr(r, "_classify_category", lambda q: _ready(None))

    with pytest.raises(ResearchFailed) as err:
        asyncio.run(r.research("whiplash"))
    assert "kept returning the same pages" in str(err.value)
    assert "Check the search provider" not in str(err.value)   # not blamed as an outage
