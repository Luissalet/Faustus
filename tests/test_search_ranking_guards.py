"""Explainable, deterministic ranking guards: score_reasons, multi-engine
reciprocal-rank fusion, anti-junk (zero-overlap + brand/shop collision), and
the freshness window for time-sensitive queries.

See services/search/ranking.py for the implementation notes; this file only
covers the new behaviour, not the pre-existing relevance/authority/recency
scoring (that's tests/test_search_ranking*.py).
"""

from datetime import datetime, timedelta

import services.search.ranking as ranking
from services.search.ranking import rank_search_results


# ---------------------------------------------------------------------------
# 1. Explainability
# ---------------------------------------------------------------------------

def test_every_result_carries_score_and_reasons():
    results = [
        {"title": "Python docs", "url": "https://docs.python.org/3/", "snippet": "Official Python documentation."},
        {"title": "Other page", "url": "https://example.org/x", "snippet": "Something else entirely unrelated."},
    ]
    ranked = rank_search_results("python", results)
    for item in ranked:
        assert isinstance(item["score"], float)
        assert isinstance(item["score_reasons"], list)
        assert all(isinstance(r, str) for r in item["score_reasons"])


# ---------------------------------------------------------------------------
# 2. Multi-engine agreement (RRF)
# ---------------------------------------------------------------------------

def test_multi_engine_beats_single_engine_at_equal_relevance():
    # Identical title/snippet/domain/age -> only engine agreement differs.
    results = [
        {"title": "Rust programming language", "url": "https://example.com/rust-a",
         "snippet": "Rust is a systems programming language.", "engines": ["duckduckgo"]},
        {"title": "Rust programming language", "url": "https://example.com/rust-b",
         "snippet": "Rust is a systems programming language.", "engines": ["duckduckgo", "brave", "bing"]},
    ]
    ranked = rank_search_results("rust programming language", results)
    assert ranked[0]["url"] == "https://example.com/rust-b"
    assert any("multi-engine" in r for r in ranked[0]["score_reasons"])
    assert not any("multi-engine" in r for r in ranked[1]["score_reasons"])


def test_multi_engine_boost_is_bounded():
    results = [
        {"title": "Topic", "url": "https://example.com/x", "snippet": "About the topic.",
         "engines": ["a", "b", "c", "d", "e", "f", "g", "h"]},
    ]
    ranked = rank_search_results("topic", results)
    # The RRF contribution alone (score minus what the same result would score
    # with one engine) must stay under the documented cap.
    single = rank_search_results(
        "topic",
        [{"title": "Topic", "url": "https://example.com/x", "snippet": "About the topic.", "engines": ["a"]}],
    )
    assert ranked[0]["score"] - single[0]["score"] <= ranking._RRF_CAP + 1e-6


def test_dedupe_by_canonical_url_merges_engines():
    # Same page (differ only by tracking param / trailing slash / case), seen
    # via two different providers -- must fuse into one ranked result whose
    # engine count reflects both.
    results = [
        {"title": "Rust", "url": "https://Example.com/rust?utm_source=x", "snippet": "Rust language.",
         "_engine": "duckduckgo"},
        {"title": "Rust", "url": "https://example.com/rust/", "snippet": "Rust language.",
         "_engine": "brave"},
    ]
    ranked = rank_search_results("rust", results)
    assert len(ranked) == 1
    assert any("multi-engine x2" in r for r in ranked[0]["score_reasons"])


# ---------------------------------------------------------------------------
# 3a. Zero-overlap demotion
# ---------------------------------------------------------------------------

def test_zero_overlap_result_demoted_below_overlapping_ones():
    results = [
        {"title": "Completely unrelated page", "url": "https://example.com/unrelated",
         "snippet": "Talks about something with no shared vocabulary."},
        {"title": "Solar panel efficiency", "url": "https://example.com/solar",
         "snippet": "New research on solar panel efficiency gains."},
    ]
    ranked = rank_search_results("solar panel efficiency", results)
    assert ranked[0]["url"] == "https://example.com/solar"
    assert ranked[-1]["url"] == "https://example.com/unrelated"
    assert "no term overlap" in ranked[-1]["score_reasons"]
    assert "no term overlap" not in ranked[0]["score_reasons"]


def test_all_zero_overlap_keeps_input_order():
    results = [
        {"title": "Alpha page", "url": "https://example.com/alpha", "snippet": "Nothing on topic here today."},
        {"title": "Alpha page", "url": "https://example.com/beta", "snippet": "Nothing on topic here today."},
    ]
    ranked = rank_search_results("solar panel efficiency", results)
    # Neither shares any content term with the query -> guard is a no-op;
    # with identical base scores the stable sort keeps input order.
    assert [r["url"] for r in ranked] == ["https://example.com/alpha", "https://example.com/beta"]
    assert not any("no term overlap" in r["score_reasons"] for r in ranked)


# ---------------------------------------------------------------------------
# 3b. Brand/shop collision guard
# ---------------------------------------------------------------------------

def test_brand_collision_demotes_shop_domain_for_short_query():
    results = [
        {"title": "Python docs", "url": "https://docs.python.org/3/", "snippet": "The official Python documentation."},
        {"title": "Python gear", "url": "https://python.shop/", "snippet": "Buy python-branded merchandise."},
    ]
    ranked = rank_search_results("python", results)
    assert ranked[0]["url"] == "https://docs.python.org/3/"
    assert any("brand-collision" in r for r in ranked[-1]["score_reasons"])


def test_purchase_intent_query_not_penalized():
    results = [
        {"title": "Buy running shoes", "url": "https://example-shop.shop/shoes", "snippet": "Buy running shoes online, best price."},
        {"title": "Running shoes guide", "url": "https://example.org/guide", "snippet": "A guide about running shoes."},
    ]
    ranked = rank_search_results("buy shoes", results)
    for item in ranked:
        assert not any("brand-collision" in r for r in item["score_reasons"])


# ---------------------------------------------------------------------------
# 4. Freshness window
# ---------------------------------------------------------------------------

def test_freshness_demotes_stale_result_for_time_sensitive_query():
    now = datetime.now()
    fresh_date = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    stale_date = (now - timedelta(days=40)).strftime("%Y-%m-%d")
    results = [
        {"title": "Champions League score", "url": "https://a.example.com/1",
         "snippet": "Latest Champions League score report.", "age": stale_date},
        {"title": "Champions League score", "url": "https://a.example.com/2",
         "snippet": "Latest Champions League score report.", "age": fresh_date},
        {"title": "Champions League score", "url": "https://a.example.com/3",
         "snippet": "Latest Champions League score report.", "age": fresh_date},
        {"title": "Champions League score", "url": "https://a.example.com/4",
         "snippet": "Latest Champions League score report.", "age": fresh_date},
    ]
    ranked = rank_search_results("who won the Champions League score today", results)
    stale_item = next(r for r in ranked if r["url"] == "https://a.example.com/1")
    assert any(reason.startswith("stale") for reason in stale_item["score_reasons"])
    # It must rank behind the fresh duplicates.
    assert ranked[-1]["url"] == "https://a.example.com/1"


def test_freshness_does_nothing_for_timeless_query():
    old_date = "2020-01-01"
    results = [
        {"title": "Python tutorial", "url": "https://example.com/tut", "snippet": "Learn Python basics.", "age": old_date},
    ]
    ranked = rank_search_results("python tutorial basics", results)
    assert not any(reason.startswith("stale") for reason in ranked[0]["score_reasons"])


def test_freshness_fallback_keeps_at_least_three_on_top_when_most_are_stale():
    now = datetime.now()
    stale_date = (now - timedelta(days=60)).strftime("%Y-%m-%d")
    fresh_date = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    results = [
        {"title": "Weather report city one", "url": "https://example.com/1", "snippet": "weather forecast report", "age": stale_date},
        {"title": "Weather report city two", "url": "https://example.com/2", "snippet": "weather forecast report", "age": stale_date},
        {"title": "Weather report city three", "url": "https://example.com/3", "snippet": "weather forecast report", "age": fresh_date},
    ]
    # Only one non-stale result (< 3) -> conservative fallback: no demotion,
    # nothing removed, all three results still present.
    ranked = rank_search_results("weather forecast today", results)
    assert len(ranked) == 3
    assert not any(reason.startswith("stale") for item in ranked for reason in item["score_reasons"])


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------

def test_ranking_is_deterministic():
    results = [
        {"title": "Rust programming language", "url": "https://example.com/rust-a",
         "snippet": "Rust is a systems programming language.", "engines": ["duckduckgo"]},
        {"title": "Rust programming language", "url": "https://example.com/rust-b",
         "snippet": "Rust is a systems programming language.", "engines": ["duckduckgo", "brave"]},
        {"title": "Unrelated", "url": "https://example.com/x", "snippet": "Something else."},
    ]
    first = rank_search_results("rust programming language", results)
    second = rank_search_results("rust programming language", results)
    assert [r["url"] for r in first] == [r["url"] for r in second]
    assert [r["score"] for r in first] == [r["score"] for r in second]
