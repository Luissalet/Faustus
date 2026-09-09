"""A result count is not search health: who answered is.

09-09-2026: every SearXNG query "returned 10 results" and all ten came from
bing, which hands back the same pages for any phrasing of a topic; mojeek
was suspended and presearch timed out. The run reported a working search
and read nothing new. The provider now records which engines put results on
the table, the route exposes it, and a research run warns once.
"""
import asyncio

from services.search import providers
from src.deep_research import DeepResearcher


def _data(engines_per_result, unresponsive):
    return {
        "results": [{"url": f"https://x/{i}", "title": "t", "content": "c", "engines": e}
                    for i, e in enumerate(engines_per_result)],
        "unresponsive_engines": unresponsive,
    }


def test_the_record_names_who_answered_who_is_down_and_who_kept_quiet():
    providers._record_engine_health(
        "whiplash classification", "bing,yandex,openalex,mojeek",
        _data([["bing"], ["bing"], ["bing", "yandex"]], [["mojeek", "Suspended: access denied"]]),
    )
    h = providers.searxng_engine_health()
    assert h["answered"] == {"bing": 3, "yandex": 1}
    assert h["unresponsive"] == [{"engine": "mojeek", "reason": "Suspended: access denied"}]
    assert h["silent"] == ["openalex"]
    assert h["results"] == 3 and h["requested"] == ["bing", "yandex", "openalex", "mojeek"]


def test_one_engine_carrying_everything_is_logged_once_per_state(caplog):
    providers._LAST_ENGINE_WARNING = ""
    only_bing = _data([["bing"]] * 10, [["mojeek", "access denied"], ["presearch", "timeout"]])
    with caplog.at_level("WARNING", logger="services.search.providers"):
        providers._record_engine_health("q1", "bing,mojeek,presearch", only_bing)
        providers._record_engine_health("q2", "bing,mojeek,presearch", only_bing)
    warnings = [r.message for r in caplog.records if r.levelname == "WARNING"]
    assert warnings == ["SearXNG: only bing answered; mojeek (access denied), presearch (timeout)"]


def test_a_research_run_warns_the_screen_once(monkeypatch):
    providers._record_engine_health(
        "q", "bing,yandex", _data([["bing"]] * 5, [["yandex", "Suspended: too many requests"]]),
    )
    r = DeepResearcher(llm_endpoint="http://127.0.0.1:11434/v1/chat/completions", llm_model="m")
    monkeypatch.setattr(r, "_active_search_provider", lambda: "searxng")
    events = []
    r._progress = events.append
    r._warn_if_single_engine()
    r._warn_if_single_engine()
    warnings = [e for e in events if e.get("phase") == "warning"]
    assert len(warnings) == 1
    assert warnings[0]["message"] == "Search is running on one engine (bing) — yandex: Suspended: too many requests"


def test_a_healthy_search_says_nothing(monkeypatch):
    providers._record_engine_health("q", "bing,yandex", _data([["bing"], ["yandex"]], []))
    r = DeepResearcher(llm_endpoint="http://127.0.0.1:11434/v1/chat/completions", llm_model="m")
    monkeypatch.setattr(r, "_active_search_provider", lambda: "searxng")
    events = []
    r._progress = events.append
    r._warn_if_single_engine()
    assert events == []
