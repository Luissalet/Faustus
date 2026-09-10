"""Lot 36, requirement 7: `services/search/providers.py::searxng_search_api`
used to drop SearXNG's own per-result `engine`/`engines` fields when
building the consumer-facing {title, url, snippet} dict. That silently broke
RES-03 provenance downstream: `src/deep_research.py::_search` tags each
result with `r.setdefault("_engine", prov)`, which only ever fires because
`_parse_results` never set the more specific `_engine` key first — so every
SearXNG-sourced citation was attributed to the generic "searxng" provider
name instead of the actual sub-engine (e.g. "bing") that found it.
"""
from __future__ import annotations

from services.search import providers


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_searxng_results_carry_engine_provenance(monkeypatch):
    monkeypatch.setattr(providers, "_get_search_instance", lambda: "http://searx.test")
    monkeypatch.setattr(providers, "_get_search_settings", lambda: {})
    monkeypatch.setattr(
        providers.httpx, "get",
        lambda url, **kwargs: _Response({
            "results": [
                {
                    "title": "Merged result",
                    "url": "https://example.com/a",
                    "content": "Snippet A",
                    "engine": "bing",
                    "engines": ["bing", "google"],
                },
                {
                    "title": "Single-engine result",
                    "url": "https://example.com/b",
                    "content": "Snippet B",
                    "engine": "duckduckgo",
                },
                {
                    "title": "No engine named",
                    "url": "https://example.com/c",
                    "content": "Snippet C",
                },
            ]
        }),
    )

    results = providers.searxng_search_api("odysseus", count=3)

    assert len(results) == 3
    assert results[0]["_engine"] == "bing"
    assert results[0]["_engines"] == ["bing", "google"]
    assert results[1]["_engine"] == "duckduckgo"
    assert "_engines" not in results[1]
    # A result with no engine info at all must not gain a fabricated one —
    # deep_research.py's own `setdefault("_engine", prov)` fallback is what
    # fills this case in, not this layer.
    assert "_engine" not in results[2]
    assert "_engines" not in results[2]
    # The pre-existing fields this lot must not disturb.
    assert results[0]["title"] == "Merged result"
    assert results[0]["url"] == "https://example.com/a"
    assert results[0]["snippet"] == "Snippet A"


def test_deep_research_setdefault_never_overrides_a_specific_engine():
    """The other half of why this matters, without re-driving the whole
    async research loop: `src/deep_research.py::_search` tags each result
    with `r.setdefault("_engine", prov)` precisely so a MORE specific engine
    `_parse_results` already set (this lot's fix) is kept, and only a result
    with none gets the generic provider name as a fallback."""
    results = [
        {"title": "specific", "url": "https://example.com/a", "_engine": "bing"},
        {"title": "generic", "url": "https://example.com/b"},
    ]
    for r in results:
        r.setdefault("_engine", "searxng")
    assert results[0]["_engine"] == "bing"
    assert results[1]["_engine"] == "searxng"
