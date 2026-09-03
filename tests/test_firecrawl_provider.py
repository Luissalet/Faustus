"""Firecrawl provider: failure is normal, and it must always be survivable.

Firecrawl is a *self-hosted* appliance here. Two properties follow from that
and are what these tests pin:

1. It is routinely absent. A stopped container, a wrong port, a blanked URL
   field — every one of those must come back as a ``success: False`` dict that
   names the cause, never as an exception. Deep research calls this per source;
   a raise would cost a source at best and the run at worst.
2. It must never reach out to api.firecrawl.dev. The whole reason to run the
   appliance locally is that a local-first user's queries stay local, so there
   is deliberately no hosted fallback to "improve" the down case with.

The live path (a real appliance answering /v2/scrape and /v2/search) is not
exercised anywhere in this suite — there is no Firecrawl to test against — so
these mock the HTTP layer and pin the contract around it.
"""

import json
import logging

import httpx
import pytest

from services.search import providers


API_KEY = "fc-supersecret-do-not-log"


class _Resp:
    """Minimal stand-in for an httpx response."""

    def __init__(self, payload=None, exc=None):
        self._payload = payload if payload is not None else {}
        self._exc = exc

    def raise_for_status(self):
        if self._exc:
            raise self._exc

    def json(self):
        return self._payload


class _Recorder:
    """Callable that records how it was called, for header + no-call assertions."""

    def __init__(self, result=None, exc=None):
        self.calls = []
        self._result = result
        self._exc = exc

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self._exc:
            raise self._exc
        return self._result if self._result is not None else _Resp()


_OK_SCRAPE = {
    "data": {
        "markdown": "# Title\n\nSome rendered body text.",
        "metadata": {
            "title": "A Rendered Page",
            "sourceURL": "https://example.com/final",
            "ogImage": "https://example.com/card.png",
        },
    }
}


@pytest.fixture
def configured(monkeypatch):
    """A configured appliance with an API key set."""
    monkeypatch.setattr(
        providers, "_get_search_settings",
        lambda: {"firecrawl_url": "http://localhost:3002", "firecrawl_api_key": API_KEY},
        raising=False,
    )


# ── The endpoint is local, and stays local ──

def test_no_hosted_fallback_when_unconfigured(monkeypatch):
    # With nothing configured at all the default is the loopback appliance.
    # If this ever resolves to Firecrawl's hosted API, a local-first user's
    # queries are silently leaving their machine.
    monkeypatch.setattr(providers, "_get_search_settings", lambda: {}, raising=False)
    monkeypatch.delenv("FIRECRAWL_API_URL", raising=False)
    instance = providers._get_firecrawl_instance()
    assert instance == "http://localhost:3002"
    assert "firecrawl.dev" not in instance


def test_blank_endpoint_skips_the_network_entirely(monkeypatch):
    # An operator who clears the URL field leaves whitespace behind, which
    # strips to "". That must short-circuit, not fire a request at a bad host.
    monkeypatch.setattr(providers, "_get_search_settings",
                        lambda: {"firecrawl_url": "   "}, raising=False)
    post = _Recorder()
    monkeypatch.setattr(providers.httpx, "post", post)

    result = providers.firecrawl_scrape("https://example.com")

    assert result["success"] is False
    assert result["error"] == "no local Firecrawl endpoint configured"
    assert post.calls == [], "must not attempt a request with no endpoint"


def test_blank_endpoint_search_returns_empty_without_calling(monkeypatch):
    monkeypatch.setattr(providers, "_get_search_settings",
                        lambda: {"firecrawl_url": "/"}, raising=False)
    post = _Recorder()
    monkeypatch.setattr(providers.httpx, "post", post)

    assert providers.firecrawl_search("anything") == []
    assert post.calls == []


# ── Scrape: the success shape ──

def test_scrape_success_shape(monkeypatch, configured):
    monkeypatch.setattr(providers.httpx, "post", _Recorder(_Resp(_OK_SCRAPE)))

    result = providers.firecrawl_scrape("https://example.com")

    assert result["success"] is True
    assert result["provider"] == "firecrawl"
    assert result["title"] == "A Rendered Page"
    assert result["content"] == "# Title\n\nSome rendered body text."
    assert result["error"] == ""
    # sourceURL wins over the requested URL so redirects are attributed right.
    assert result["url"] == "https://example.com/final"
    assert result["og_image"] == "https://example.com/card.png"


def test_scrape_posts_to_the_local_v2_endpoint(monkeypatch, configured):
    post = _Recorder(_Resp(_OK_SCRAPE))
    monkeypatch.setattr(providers.httpx, "post", post)

    providers.firecrawl_scrape("https://example.com", timeout=90)

    (url, ), kwargs = post.calls[0]
    assert url == "http://localhost:3002/v2/scrape"
    assert kwargs["json"]["url"] == "https://example.com"
    assert kwargs["json"]["formats"] == ["markdown"]
    assert kwargs["timeout"] == 90


# ── Scrape: every failure is a dict, never a raise ──

@pytest.mark.parametrize("exc, cause", [
    (httpx.HTTPStatusError("Server error '502 Bad Gateway'", request=None, response=None), "502"),
    (httpx.ReadTimeout("timed out waiting for the appliance"), "timed out"),
    (httpx.ConnectError("[Errno 111] Connection refused"), "refused"),
])
def test_scrape_transport_failures_are_reported_not_raised(monkeypatch, configured, exc, cause):
    monkeypatch.setattr(providers.httpx, "post", _Recorder(exc=exc))

    result = providers.firecrawl_scrape("https://example.com")

    assert result["success"] is False
    assert result["content"] == ""
    assert cause in result["error"], "the error must name what actually went wrong"


def test_scrape_non_2xx_is_reported_not_raised(monkeypatch, configured):
    err = httpx.HTTPStatusError("Client error '404 Not Found'", request=None, response=None)
    monkeypatch.setattr(providers.httpx, "post", _Recorder(_Resp(_OK_SCRAPE, exc=err)))

    result = providers.firecrawl_scrape("https://example.com")

    assert result["success"] is False
    assert "404" in result["error"]


@pytest.mark.parametrize("payload", [
    {},
    {"data": {}},
    {"data": {"markdown": ""}},
    {"data": {"markdown": "   \n  "}},
    {"data": {"markdown": None}},
    {"data": None},
])
def test_scrape_200_without_markdown_is_a_failure(monkeypatch, configured, payload):
    # A 200 carrying no usable text is worthless to the extractor. Reporting it
    # as success would hand deep research an empty page instead of letting it
    # fall back to the native fetcher.
    monkeypatch.setattr(providers.httpx, "post", _Recorder(_Resp(payload)))

    result = providers.firecrawl_scrape("https://example.com")

    assert result["success"] is False
    assert "no markdown content" in result["error"]


def test_scrape_malformed_json_is_reported_not_raised(monkeypatch, configured):
    class _BadJSON(_Resp):
        def json(self):
            raise json.JSONDecodeError("Expecting value", "<html>down</html>", 0)

    monkeypatch.setattr(providers.httpx, "post", _Recorder(_BadJSON()))

    result = providers.firecrawl_scrape("https://example.com")
    assert result["success"] is False


# ── The API key: sent in a header, and nowhere else ──

def test_api_key_travels_in_the_authorization_header(monkeypatch, configured):
    post = _Recorder(_Resp(_OK_SCRAPE))
    monkeypatch.setattr(providers.httpx, "post", post)

    providers.firecrawl_scrape("https://example.com")
    providers.firecrawl_search("query")

    for _args, kwargs in post.calls:
        assert kwargs["headers"]["Authorization"] == f"Bearer {API_KEY}"
        # Never in the URL or body, where proxies and access logs would see it.
        assert API_KEY not in str(_args)
        assert API_KEY not in json.dumps(kwargs["json"])


def test_no_authorization_header_when_no_key_configured(monkeypatch):
    monkeypatch.setattr(providers, "_get_search_settings",
                        lambda: {"firecrawl_url": "http://localhost:3002"}, raising=False)
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    post = _Recorder(_Resp(_OK_SCRAPE))
    monkeypatch.setattr(providers.httpx, "post", post)

    providers.firecrawl_scrape("https://example.com")

    assert "Authorization" not in post.calls[0][1]["headers"]


@pytest.mark.parametrize("outcome", ["success", "http_error", "no_markdown"])
def test_api_key_never_reaches_a_log_record_or_the_result(monkeypatch, configured, caplog, outcome):
    # A leaked credential in a log file outlives the process that wrote it, and
    # the returned dict is handed to the research layer and the UI.
    responses = {
        "success": _Recorder(_Resp(_OK_SCRAPE)),
        "http_error": _Recorder(exc=httpx.ConnectError("[Errno 111] Connection refused")),
        "no_markdown": _Recorder(_Resp({"data": {"markdown": ""}})),
    }
    monkeypatch.setattr(providers.httpx, "post", responses[outcome])
    caplog.set_level(logging.DEBUG, logger="services.search.providers")

    scraped = providers.firecrawl_scrape("https://example.com")
    searched = providers.firecrawl_search("a query")

    assert caplog.records, "the test is worthless if nothing was logged"
    for record in caplog.records:
        assert API_KEY not in record.getMessage()
        assert API_KEY not in str(record.args)
    assert API_KEY not in json.dumps(scraped)
    assert API_KEY not in json.dumps(searched)


# ── Search: same result shape as every other provider ──

_OK_SEARCH = {
    "data": {
        "web": [
            {"title": "First", "url": "https://a.example/1", "description": "a snippet"},
            {"title": "Second", "url": "https://b.example/2", "markdown": "body as snippet"},
        ]
    }
}


def test_search_result_shape_matches_searxng(monkeypatch):
    # Compare against this tree's own SearXNG provider rather than a literal:
    # if the house result shape changes, this test moves with it.
    monkeypatch.setattr(providers, "_get_search_settings",
                        lambda: {"firecrawl_url": "http://localhost:3002"}, raising=False)
    monkeypatch.setattr(providers.httpx, "post", _Recorder(_Resp(_OK_SEARCH)))
    monkeypatch.setattr(providers.httpx, "get", _Recorder(_Resp(
        {"results": [{"title": "S", "url": "https://s.example", "content": "snip"}]}
    )))

    firecrawl_results = providers.firecrawl_search("python packaging")
    searxng_results = providers.searxng_search_api("python packaging")

    assert firecrawl_results and searxng_results
    assert {k for k in firecrawl_results[0]} == {k for k in searxng_results[0]}
    assert firecrawl_results[0] == {
        "title": "First", "url": "https://a.example/1", "snippet": "a snippet",
    }
    # description missing → markdown is used as the snippet.
    assert firecrawl_results[1]["snippet"] == "body as snippet"


def test_search_drops_entries_without_a_url(monkeypatch):
    monkeypatch.setattr(providers, "_get_search_settings",
                        lambda: {"firecrawl_url": "http://localhost:3002"}, raising=False)
    monkeypatch.setattr(providers.httpx, "post", _Recorder(_Resp(
        {"data": {"web": [{"title": "no url"}, "not-a-dict",
                          {"title": "ok", "url": "https://ok.example"}]}}
    )))

    results = providers.firecrawl_search("q")

    assert [r["url"] for r in results] == ["https://ok.example"]


@pytest.mark.parametrize("exc", [
    httpx.ConnectError("Connection refused"),
    httpx.ReadTimeout("timed out"),
    httpx.HTTPStatusError("500", request=None, response=None),
])
def test_search_failures_return_empty_not_raise(monkeypatch, exc):
    # An empty list lets core.py continue down the configured fallback chain.
    monkeypatch.setattr(providers, "_get_search_settings",
                        lambda: {"firecrawl_url": "http://localhost:3002"}, raising=False)
    monkeypatch.setattr(providers.httpx, "post", _Recorder(exc=exc))

    assert providers.firecrawl_search("q") == []


def test_search_time_filter_maps_to_tbs(monkeypatch):
    monkeypatch.setattr(providers, "_get_search_settings",
                        lambda: {"firecrawl_url": "http://localhost:3002"}, raising=False)
    post = _Recorder(_Resp(_OK_SEARCH))
    monkeypatch.setattr(providers.httpx, "post", post)

    providers.firecrawl_search("q", count=3, time_filter="week")

    body = post.calls[0][1]["json"]
    assert body["tbs"] == "qdr:w"
    assert body["limit"] == 3


# ── Registry + dispatch wiring ──

def test_registered_as_a_url_provider_needing_no_key():
    label, needs_key, needs_url = providers.PROVIDER_INFO["firecrawl"]
    assert "self-hosted" in label
    assert needs_key is False and needs_url is True


def test_core_dispatches_firecrawl(monkeypatch):
    from services.search import core

    monkeypatch.setattr(core, "firecrawl_search",
                        lambda q, c, t=None: [{"title": "t", "url": "u", "snippet": "s"}])
    assert core._call_provider("firecrawl", "q", 5) == [
        {"title": "t", "url": "u", "snippet": "s"}
    ]


def test_search_config_exposes_url_but_never_the_key(monkeypatch):
    # firecrawl_api_key is named like every other provider key precisely so the
    # existing name-shaped secret rules cover it without new wiring.
    from services.search import core
    from src.settings_scrub import is_secret_key, scrub_settings

    monkeypatch.setattr(core, "_get_search_settings", lambda: {
        "search_provider": "firecrawl",
        "firecrawl_url": "http://localhost:3002",
        "firecrawl_api_key": API_KEY,
    }, raising=False)

    config = core.get_search_config()

    assert config["active_provider"] == "firecrawl"
    assert config["firecrawl_url"] == "http://localhost:3002"
    assert API_KEY not in json.dumps(config)
    assert is_secret_key("firecrawl_api_key") is True
    assert scrub_settings({"firecrawl_api_key": API_KEY})["firecrawl_api_key"] == ""


# ── Deep research: a down appliance degrades the run, it does not end it ──

def _researcher(monkeypatch, provider="firecrawl"):
    """A DeepResearcher with only the attributes _fetch_and_extract touches.

    Built with __new__ so the real __init__ (LLM client, timers, citation
    registry) stays out of the way.
    """
    from src.deep_research import DeepResearcher

    r = DeepResearcher.__new__(DeepResearcher)
    # An explicit override short-circuits provider resolution, so this needs no
    # settings store.
    r.search_provider_override = provider
    r.extraction_timeout = 90
    r.max_content_chars = 15000
    r.urls_fetched = set()
    r._progress = None

    async def _llm(messages, **kwargs):
        return json.dumps({"summary": "a real finding", "evidence": "quoted text"})

    r._llm = _llm
    return r


def _install_search_module(monkeypatch, *, scrape, fetch):
    import sys
    import types

    fake = types.ModuleType("src.search")
    fake.firecrawl_scrape = scrape
    fake.fetch_webpage_content = fetch
    monkeypatch.setitem(sys.modules, "src.search", fake)


_NATIVE_PAGE = {
    "success": True,
    "content": "Native fetcher body text, long enough to extract from.",
    "title": "Native Title",
    "og_image": "",
}


@pytest.mark.parametrize("scrape_result", [
    {"success": False, "content": "", "error": "[Errno 111] Connection refused"},
    {"success": True, "content": "", "error": ""},
    {"success": False, "content": "", "error": "no local Firecrawl endpoint configured"},
])
def test_research_falls_back_to_native_fetcher_when_scrape_fails(monkeypatch, caplog, scrape_result):
    import asyncio

    native_calls = []

    def _fetch(url, timeout):
        native_calls.append(url)
        return dict(_NATIVE_PAGE)

    _install_search_module(monkeypatch, scrape=lambda url, timeout: dict(scrape_result), fetch=_fetch)
    caplog.set_level(logging.WARNING, logger="src.deep_research")

    finding = asyncio.run(
        _researcher(monkeypatch)._fetch_and_extract("https://example.com", "why?", "T")
    )

    # The run continues: the source still produces a finding, from the fallback.
    assert native_calls == ["https://example.com"]
    assert finding is not None
    assert finding["summary"] == "a real finding"
    assert finding["title"] == "T"
    # And the warning says why the appliance was skipped, so an operator whose
    # container is down can tell that from a run that simply found nothing.
    warnings = " ".join(r.getMessage() for r in caplog.records)
    assert "Firecrawl" in warnings and "falling back" in warnings


def test_research_uses_firecrawl_content_when_the_scrape_works(monkeypatch):
    import asyncio

    native_calls = []
    scraped = {
        "success": True,
        "content": "Appliance-rendered body text.",
        "title": "Rendered",
        "og_image": "https://example.com/card.png",
    }
    _install_search_module(
        monkeypatch,
        scrape=lambda url, timeout: dict(scraped),
        fetch=lambda url, timeout: native_calls.append(url) or dict(_NATIVE_PAGE),
    )

    finding = asyncio.run(
        _researcher(monkeypatch)._fetch_and_extract("https://example.com", "why?", "")
    )

    assert native_calls == [], "a good scrape must not also pay for a native fetch"
    assert finding["og_image"] == "https://example.com/card.png"


def test_research_skips_firecrawl_entirely_for_other_providers(monkeypatch):
    import asyncio

    scrape_calls = []
    _install_search_module(
        monkeypatch,
        scrape=lambda url, timeout: scrape_calls.append(url) or {"success": True, "content": "x"},
        fetch=lambda url, timeout: dict(_NATIVE_PAGE),
    )

    asyncio.run(
        _researcher(monkeypatch, provider="searxng")._fetch_and_extract("https://e.com", "q", "")
    )

    assert scrape_calls == []


def test_research_returns_none_when_both_fetchers_fail(monkeypatch):
    import asyncio

    _install_search_module(
        monkeypatch,
        scrape=lambda url, timeout: {"success": False, "content": "", "error": "down"},
        fetch=lambda url, timeout: {"success": False, "content": ""},
    )

    finding = asyncio.run(
        _researcher(monkeypatch)._fetch_and_extract("https://example.com", "q", "")
    )

    assert finding is None


def test_research_survives_a_native_fetcher_that_raises(monkeypatch):
    import asyncio

    def _boom(url, timeout):
        raise RuntimeError("socket exploded")

    _install_search_module(
        monkeypatch,
        scrape=lambda url, timeout: {"success": False, "content": "", "error": "down"},
        fetch=_boom,
    )

    # One dead source must not propagate out of the extractor.
    finding = asyncio.run(
        _researcher(monkeypatch)._fetch_and_extract("https://example.com", "q", "")
    )

    assert finding is None
