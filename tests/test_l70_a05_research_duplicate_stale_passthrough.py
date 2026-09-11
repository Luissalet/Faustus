"""Lote 70a, punto A.5 — `src/research_handler.py::_extract_sources` /
`_extract_raw_findings` must copy `duplicate_of`, `stale` and `age_days`
from a finding onto the entry it produces. `src/deep_research.py` already
stamps these three keys on findings (WEB-02), and
`studio/src/adapters/research.ts::sourceFrom` already decodes them off
`ResearchSource` — the passthrough in between was the only missing piece
(see that adapter's own doc comment: "Requires the backend passthrough
described in this lote's report").
"""
from __future__ import annotations

from src.research_handler import ResearchHandler


def test_extract_sources_copies_duplicate_and_stale_signals():
    findings = [
        {
            "url": "http://a", "title": "A", "summary": "Solid detail about the topic",
            "duplicate_of": "http://original", "stale": True, "age_days": 42,
        },
        {"url": "http://b", "title": "B", "summary": "Other solid detail"},
    ]
    out = ResearchHandler._extract_sources(findings)
    by_url = {s["url"]: s for s in out}

    assert by_url["http://a"]["duplicate_of"] == "http://original"
    assert by_url["http://a"]["stale"] is True
    assert by_url["http://a"]["age_days"] == 42
    # A finding with no signal at all gets no spurious keys — undefined on
    # the wire, not a false "fresh"/"not a duplicate".
    assert "duplicate_of" not in by_url["http://b"]
    assert "stale" not in by_url["http://b"]
    assert "age_days" not in by_url["http://b"]


def test_extract_sources_keeps_stale_false_distinct_from_absent():
    findings = [{"url": "http://c", "title": "C", "summary": "Good content", "stale": False}]
    out = ResearchHandler._extract_sources(findings)
    assert out[0]["stale"] is False


def test_extract_raw_findings_copies_duplicate_and_stale_signals():
    findings = [
        {
            "url": "http://a", "title": "A", "summary": "Solid detail about the topic",
            "duplicate_of": "http://original", "stale": True, "age_days": 7.5,
        },
    ]
    out = ResearchHandler._extract_raw_findings(findings)
    assert out[0]["duplicate_of"] == "http://original"
    assert out[0]["stale"] is True
    assert out[0]["age_days"] == 7.5
