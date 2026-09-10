"""Lote 64 — WEB-02: "duplicate page" and "stale content" signals on a web
read result.

`src/outbound_fetch.py` gains two pure signals: `staleness_from_headers`
(a response's own Last-Modified/Date+Age headers) and
`content_fingerprint`/`find_duplicate` (the same content syndicated under a
different URL). `src/deep_research.py` wires the duplicate signal end to end
— a finding whose content already arrived this run under another URL is
stamped `duplicate_of` in `_search_and_extract`.

Integration note (see the batch report): staleness is not wired into
`deep_research.py` because `services/search/content.py::fetch_webpage_content`
(not in this lot's file list) does not surface response headers to its
caller today — `staleness_from_headers` is ready for that one-line addition.
"""
import asyncio
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from src.deep_research import DeepResearcher
from src.outbound_fetch import (
    STALE_AFTER_DAYS,
    content_fingerprint,
    find_duplicate,
    staleness_from_headers,
)


# ── staleness_from_headers ──────────────────────────────────────────────

def _http_date(dt: datetime) -> str:
    return dt.strftime("%a, %d %b %Y %H:%M:%S GMT")


def test_a_recent_last_modified_is_not_stale():
    now = datetime(2026, 9, 10, tzinfo=timezone.utc)
    headers = httpx.Headers({"last-modified": _http_date(now - timedelta(days=5))})
    signal = staleness_from_headers(headers, now=now)
    assert signal == {"stale": False, "age_days": 5.0, "basis": "last-modified"}


def test_an_old_last_modified_is_stale():
    now = datetime(2026, 9, 10, tzinfo=timezone.utc)
    headers = httpx.Headers({"last-modified": _http_date(now - timedelta(days=STALE_AFTER_DAYS + 30))})
    signal = staleness_from_headers(headers, now=now)
    assert signal["stale"] is True
    assert signal["basis"] == "last-modified"


def test_falls_back_to_date_minus_age_when_no_last_modified():
    now = datetime(2026, 9, 10, tzinfo=timezone.utc)
    headers = httpx.Headers({
        "date": _http_date(now),
        "age": str(3 * STALE_AFTER_DAYS * 86400),  # a CDN holding a very old copy
    })
    signal = staleness_from_headers(headers, now=now)
    assert signal["stale"] is True
    assert signal["basis"] == "age"


def test_no_usable_header_is_none_not_a_false_fresh():
    assert staleness_from_headers(httpx.Headers({})) is None


def test_an_unparseable_date_is_none_not_a_crash():
    assert staleness_from_headers(httpx.Headers({"last-modified": "not a date"})) is None


def test_a_garbage_age_header_is_none_not_a_crash():
    headers = httpx.Headers({"date": _http_date(datetime.now(timezone.utc)), "age": "not-a-number"})
    assert staleness_from_headers(headers) is None


# ── content_fingerprint / find_duplicate ────────────────────────────────

def test_identical_content_fingerprints_the_same():
    a = content_fingerprint("The quick brown fox jumps over the lazy dog.")
    b = content_fingerprint("The quick brown fox jumps over the lazy dog.")
    assert a == b


def test_reflowed_whitespace_still_matches():
    a = content_fingerprint("Hello   world.\n\nThis is   a page.")
    b = content_fingerprint("Hello world. This is a page.")
    assert a == b


def test_different_content_fingerprints_differently():
    a = content_fingerprint("Page about whiplash treatment.")
    b = content_fingerprint("Page about a completely different topic.")
    assert a != b


def test_find_duplicate_returns_the_earlier_url():
    seen = {}
    seen[content_fingerprint("shared article text")] = "https://mirror-a.test/page"
    found = find_duplicate("shared article text", seen)
    assert found == "https://mirror-a.test/page"


def test_find_duplicate_none_for_new_content():
    assert find_duplicate("brand new text nobody has seen", {}) is None


def test_find_duplicate_none_for_empty_text():
    assert find_duplicate("", {"x": "https://a.test"}) is None


# ── wiring: DeepResearcher stamps duplicate_of on real findings ────────────

def _finding(url, title, evidence):
    return {"url": url, "title": title, "summary": "", "evidence": evidence}


def test_search_and_extract_stamps_duplicate_of_for_mirrored_content(monkeypatch):
    r = DeepResearcher("http://unused.invalid", "m")
    r._start_time = time.time()

    async def _search(query):
        return [{"url": "https://a.test/1", "title": "Original"},
                {"url": "https://mirror.test/1", "title": "Mirror"}]
    monkeypatch.setattr(r, "_search", _search)

    async def _extract(url, question, title=""):
        # Same evidence text, reflowed differently, under two different URLs.
        text = ("Whiplash recovery typically takes 6-12 weeks with "
                "appropriate physiotherapy.")
        if url == "https://mirror.test/1":
            text = "Whiplash recovery   typically takes 6-12\nweeks with appropriate physiotherapy."
        return _finding(url, title, text)
    monkeypatch.setattr(r, "_fetch_and_extract", _extract)

    findings = asyncio.run(r._search_and_extract(["whiplash recovery time"], "q?"))

    by_url = {f["url"]: f for f in findings}
    assert "duplicate_of" not in by_url["https://a.test/1"]
    assert by_url["https://mirror.test/1"]["duplicate_of"] == "https://a.test/1"


def test_search_and_extract_does_not_flag_distinct_content(monkeypatch):
    r = DeepResearcher("http://unused.invalid", "m")
    r._start_time = time.time()

    async def _search(query):
        return [{"url": "https://a.test/1", "title": "A"},
                {"url": "https://b.test/2", "title": "B"}]
    monkeypatch.setattr(r, "_search", _search)

    async def _extract(url, question, title=""):
        return _finding(url, title, f"unique content about {url}")
    monkeypatch.setattr(r, "_fetch_and_extract", _extract)

    findings = asyncio.run(r._search_and_extract(["q"], "q?"))
    assert all("duplicate_of" not in f for f in findings)


def test_a_resumed_run_seeds_content_seen_without_self_flagging():
    """A continuation's OWN prior finding must not be marked a duplicate of
    itself when `research()` re-registers it."""
    r = DeepResearcher("http://unused.invalid", "m")
    prior = [_finding("https://a.test/1", "A", "some earlier evidence text")]
    for f in prior:
        r.citations.add(f)
        r._seed_content_seen(f)
    assert "https://a.test/1" in r._content_seen.values()
    # Re-stamping the SAME finding again must not retroactively flag it.
    r._stamp_duplicate(prior[0])
    assert "duplicate_of" not in prior[0]
