"""WEB-01 — diversity/coverage report and explicit `degraded` on /api/search/health.

A result count hides two different failures: one engine carrying every
result (narrow, but real), and repeating the same URLs across consecutive
queries (no new coverage even though each query "succeeded"). This test
covers `diversity_report` directly, its wiring into
`providers._record_engine_health` / `ENGINE_HEALTH`, and the route exposing
`degraded`/`degraded_reason`/`diversity` explicitly instead of leaving a
caller to infer degradation from `single_engine` alone.
"""
import asyncio

from unittest.mock import MagicMock

from routes.search import search_routes
from services.search import providers
from services.search.diversity import diversity_report, overlap_ratio, unique_domains


def _results(urls_engines):
    return [{"url": u, "title": "t", "content": "c", "engines": e} for u, e in urls_engines]


def _health_endpoint():
    router = search_routes.setup_search_routes(MagicMock())
    return next(r.endpoint for r in router.routes if r.path == "/api/search/health")


# ---------------------------------------------------------------------------
# diversity_report / unique_domains / overlap_ratio — pure functions
# ---------------------------------------------------------------------------

def test_unique_domains_counts_distinct_hosts_only():
    domains = unique_domains([
        "https://a.example.com/x", "https://a.example.com/y",
        "https://b.example.com/z", "not-a-url", "",
    ])
    assert domains == ["a.example.com", "b.example.com"]


def test_overlap_ratio_of_identical_batches_is_one():
    urls = ["https://x/1", "https://x/2", "https://x/3"]
    assert overlap_ratio(urls, urls) == 1.0


def test_overlap_ratio_with_nothing_previous_is_zero_not_error():
    assert overlap_ratio(["https://x/1"], []) == 0.0
    assert overlap_ratio([], ["https://x/1"]) == 0.0


def test_diversity_report_flags_single_engine():
    report = diversity_report(_results([("https://x/1", ["bing"]), ("https://x/2", ["bing"])]))
    assert report["engines_answered"] == {"bing": 2}
    assert report["domain_count"] == 1
    assert "only one search engine" in report["coverage_warning"]


def test_diversity_report_flags_high_overlap_between_queries():
    previous = [f"https://site{i}.example/{i}" for i in range(10)]
    current = previous[:8] + ["https://new1.example/a", "https://new2.example/b"]
    report = diversity_report(_results([(u, ["bing", "yandex"]) for u in current]), previous_urls=previous)
    assert report["overlap_with_previous_query"] == 0.8
    assert "repeat the previous query" in report["coverage_warning"]


def test_diversity_report_healthy_run_has_no_warning():
    results = _results([
        ("https://a.example/1", ["bing"]),
        ("https://b.example/2", ["yandex"]),
    ])
    report = diversity_report(results, previous_urls=["https://totally-different.example/9"])
    assert report["coverage_warning"] is None
    assert report["domain_count"] == 2


# ---------------------------------------------------------------------------
# providers._record_engine_health folds diversity into ENGINE_HEALTH (rule 4:
# one store, not a second one)
# ---------------------------------------------------------------------------

def test_record_engine_health_carries_diversity_and_urls():
    providers.ENGINE_HEALTH.clear()
    data = {
        "results": [
            {"url": "https://x/1", "title": "t", "content": "c", "engines": ["bing"]},
            {"url": "https://x/2", "title": "t", "content": "c", "engines": ["bing"]},
        ],
        "unresponsive_engines": [],
    }
    providers._record_engine_health("q", "bing,yandex", data)
    h = providers.searxng_engine_health()
    assert h["urls"] == ["https://x/1", "https://x/2"]
    assert h["diversity"]["engines_answered"] == {"bing": 2}
    assert "only one search engine" in h["diversity"]["coverage_warning"]


def test_record_engine_health_overlap_against_previous_call():
    providers.ENGINE_HEALTH.clear()
    same_urls = [{"url": f"https://x/{i}", "title": "t", "content": "c", "engines": ["bing", "yandex"]}
                 for i in range(5)]
    providers._record_engine_health("q1", "bing,yandex", {"results": same_urls, "unresponsive_engines": []})
    providers._record_engine_health("q2", "bing,yandex", {"results": same_urls, "unresponsive_engines": []})
    h = providers.searxng_engine_health()
    assert h["diversity"]["overlap_with_previous_query"] == 1.0
    assert "repeat the previous query" in h["diversity"]["coverage_warning"]


# ---------------------------------------------------------------------------
# degradation_reason + GET /api/search/health degraded/degraded_reason
# ---------------------------------------------------------------------------

def test_degradation_reason_none_when_multiple_engines_answer():
    providers.ENGINE_HEALTH.clear()
    providers._record_engine_health(
        "q", "bing,yandex",
        {"results": [{"url": "https://x/1", "engines": ["bing"]},
                     {"url": "https://x/2", "engines": ["yandex"]}],
         "unresponsive_engines": []},
    )
    assert providers.degradation_reason(providers.searxng_engine_health()) is None


def test_degradation_reason_none_before_any_call():
    assert providers.degradation_reason({}) is None
    assert providers.degradation_reason(None) is None


def test_route_reports_degraded_true_with_named_cause_for_one_engine(monkeypatch):
    providers._LAST_ENGINE_WARNING = ""
    providers.ENGINE_HEALTH.clear()
    only_bing = {
        "results": [{"url": f"https://x/{i}", "title": "t", "content": "c", "engines": ["bing"]}
                    for i in range(10)],
        "unresponsive_engines": [["mojeek", "Suspended: access denied"]],
    }
    providers._record_engine_health("q", "bing,mojeek", only_bing)
    monkeypatch.setattr(providers, "_GENERAL_ENGINES", "bing,mojeek")

    endpoint = _health_endpoint()
    body = asyncio.run(endpoint())

    assert body["degraded"] is True
    assert body["degraded_reason"] == "only bing answered — mojeek (Suspended: access denied)"
    assert body["diversity"]["domain_count"] == 1


def test_route_reports_degraded_true_with_named_cause_for_no_engine(monkeypatch):
    providers._LAST_ENGINE_WARNING = ""
    providers.ENGINE_HEALTH.clear()
    nothing = {
        "results": [],
        "unresponsive_engines": [["bing", "timeout"], ["mojeek", "Suspended: access denied"]],
    }
    providers._record_engine_health("q2", "bing,mojeek", nothing)
    monkeypatch.setattr(providers, "_GENERAL_ENGINES", "bing,mojeek")

    endpoint = _health_endpoint()
    body = asyncio.run(endpoint())

    assert body["degraded"] is True
    assert body["degraded_reason"] == "no engine answered — bing (timeout), mojeek (Suspended: access denied)"


def test_route_reports_not_degraded_when_two_engines_answer(monkeypatch):
    providers._LAST_ENGINE_WARNING = ""
    providers.ENGINE_HEALTH.clear()
    healthy = {
        "results": [{"url": "https://a/1", "title": "t", "content": "c", "engines": ["bing"]},
                    {"url": "https://b/2", "title": "t", "content": "c", "engines": ["yandex"]}],
        "unresponsive_engines": [],
    }
    providers._record_engine_health("q3", "bing,yandex", healthy)
    monkeypatch.setattr(providers, "_GENERAL_ENGINES", "bing,yandex")

    endpoint = _health_endpoint()
    body = asyncio.run(endpoint())

    assert body["degraded"] is False
    assert body["degraded_reason"] is None
