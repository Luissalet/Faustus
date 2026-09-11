"""Lote 67 — ola A wiring: `services.search.content.fetch_webpage_content`
now returns `headers` (a plain, JSON-serializable dict), and
`DeepResearcher._stamp_duplicate` (src/deep_research.py) uses it to also
stamp `stale`/`age_days` via `src.outbound_fetch.staleness_from_headers` —
the integration `tests/test_l64_web02_duplicate_and_stale.py` documented as
not yet wired ("fetch_webpage_content does not surface response headers to
its caller today").

`staleness_from_headers` itself is lote 64's and untouched — this proves
only the two new things: content.py returning `headers`, and
`_stamp_duplicate` reading them (and never leaking the transient
`_fetch_headers` key onto a real finding).
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from src.deep_research import DeepResearcher


def _http_date(dt: datetime) -> str:
    return dt.strftime("%a, %d %b %Y %H:%M:%S GMT")


# ── services/search/content.py returns headers ──────────────────────────

def test_fetch_webpage_content_returns_a_plain_serializable_headers_dict(monkeypatch, tmp_path):
    import json as _json
    import services.search.content as content_mod

    monkeypatch.setattr(content_mod, "CONTENT_CACHE_DIR", tmp_path)

    class _FakeResponse:
        status_code = 200
        content = b"<html><body><main class='content'>Hello world, this is a "\
                  b"reasonably long paragraph of article text so the extractor "\
                  b"keeps it as the main content block for this fake page.</main>"\
                  b"</body></html>"
        text = content.decode()
        headers = httpx.Headers({
            "Content-Type": "text/html",
            "Last-Modified": _http_date(datetime.now(timezone.utc) - timedelta(days=3)),
        })

        def raise_for_status(self):
            return None

    monkeypatch.setattr(content_mod, "_get_public_url", lambda *a, **kw: _FakeResponse())

    result = content_mod.fetch_webpage_content("https://example.test/article")
    assert result["success"] is True
    assert isinstance(result["headers"], dict)
    # Must actually be JSON-serializable (the result is cache-written with
    # json.dump) — httpx.Headers itself is not.
    _json.dumps(result["headers"])
    assert result["headers"].get("content-type") == "text/html"
    assert "last-modified" in result["headers"]


# ── deep_research.py: _stamp_duplicate also stamps staleness ────────────

def _finding_with_headers(url, evidence, headers):
    return {"url": url, "title": "t", "summary": "", "evidence": evidence,
            "_fetch_headers": headers}


def test_stamp_duplicate_stamps_stale_and_age_days_from_fetch_headers():
    r = DeepResearcher("http://unused.invalid", "m")
    now = datetime.now(timezone.utc)
    headers = {"last-modified": _http_date(now - timedelta(days=800))}  # well past STALE_AFTER_DAYS (730)
    finding = _finding_with_headers("https://a.test/1", "some unique article text here", headers)

    r._stamp_duplicate(finding)

    assert finding["stale"] is True
    assert isinstance(finding["age_days"], float)
    assert finding["age_days"] >= 799


def test_stamp_duplicate_marks_fresh_content_not_stale():
    r = DeepResearcher("http://unused.invalid", "m")
    now = datetime.now(timezone.utc)
    headers = {"last-modified": _http_date(now - timedelta(days=2))}
    finding = _finding_with_headers("https://a.test/2", "fresh unique text", headers)

    r._stamp_duplicate(finding)

    assert finding["stale"] is False


def test_stamp_duplicate_never_leaks_the_transient_fetch_headers_key():
    r = DeepResearcher("http://unused.invalid", "m")
    finding = _finding_with_headers("https://a.test/3", "text", {"last-modified": "not a real date"})
    r._stamp_duplicate(finding)
    assert "_fetch_headers" not in finding


def test_stamp_duplicate_without_headers_does_not_add_staleness_keys():
    """A firecrawl-scraped page (no `_fetch_headers`) must not fabricate a
    staleness verdict from nothing."""
    r = DeepResearcher("http://unused.invalid", "m")
    finding = {"url": "https://a.test/4", "title": "t", "summary": "", "evidence": "text"}
    r._stamp_duplicate(finding)
    assert "stale" not in finding
    assert "age_days" not in finding


def test_stamp_duplicate_bad_headers_never_raises():
    r = DeepResearcher("http://unused.invalid", "m")
    finding = _finding_with_headers("https://a.test/5", "text", object())  # not header-shaped
    r._stamp_duplicate(finding)  # must not raise
    assert "_fetch_headers" not in finding


# ── end-to-end: _search_and_extract stamps both duplicate_of and staleness ─

def test_search_and_extract_stamps_staleness_alongside_duplicate_of(monkeypatch):
    r = DeepResearcher("http://unused.invalid", "m")
    r._start_time = time.time()

    async def _search(query):
        return [{"url": "https://a.test/1", "title": "Original"}]
    monkeypatch.setattr(r, "_search", _search)

    now = datetime.now(timezone.utc)
    stale_headers = {"last-modified": _http_date(now - timedelta(days=800))}

    async def _extract(url, question, title=""):
        return {"url": url, "title": title, "summary": "",
                "evidence": "some article text about a topic",
                "_fetch_headers": stale_headers}
    monkeypatch.setattr(r, "_fetch_and_extract", _extract)

    findings = asyncio.run(r._search_and_extract(["a query"], "q?"))
    assert len(findings) == 1
    assert findings[0]["stale"] is True
    assert "_fetch_headers" not in findings[0]
