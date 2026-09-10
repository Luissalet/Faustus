"""RES-06 — calidad de evidencia configurable por perfil.

`DeepResearcher` had no notion of an evidence-quality profile before this
lote; a report's citation COUNT was the only signal available.
"""
import asyncio

import pytest

from src.deep_research import (
    DEFAULT_QUALITY_PROFILES,
    DeepResearcher,
    EvidenceQualityProfile,
    assess_evidence_quality,
)


def _finding(url, content="some text", title="T"):
    return {"url": url, "title": title, "content": content}


def test_default_profiles_cover_general_technical_academic_clinical():
    assert set(DEFAULT_QUALITY_PROFILES) == {"general", "technical", "academic", "clinical"}
    assert DEFAULT_QUALITY_PROFILES["clinical"].min_sources_per_claim > DEFAULT_QUALITY_PROFILES["general"].min_sources_per_claim
    assert DEFAULT_QUALITY_PROFILES["clinical"].require_date is True
    assert DEFAULT_QUALITY_PROFILES["general"].require_date is False


def test_blocked_domains_are_excluded_from_usable_sources():
    profile = EvidenceQualityProfile("x", min_sources_per_claim=1, blocked_domains=("reddit.com",))
    findings = [_finding("https://reddit.com/r/x", "2024 discussion"), _finding("https://who.int/report", "2024-01-01 report")]
    result = assess_evidence_quality(findings, profile)
    assert result["usable_sources"] == 1
    assert result["blocked_excluded"] == 1


def test_preferred_domains_push_grade_toward_high():
    profile = EvidenceQualityProfile("x", min_sources_per_claim=1, preferred_domains=("who.int",))
    findings = [_finding("https://who.int/a", "2023 report"), _finding("https://who.int/b", "2023 report")]
    result = assess_evidence_quality(findings, profile)
    assert result["grade"] == "high"
    assert result["preferred_count"] == 2


# ---------------------------------------------------------------------------
# The acceptance criterion itself: citation percentage alone must never
# declare a clinical/technical report correct.
# ---------------------------------------------------------------------------

def test_regression_many_citations_without_dates_never_grades_high_for_clinical():
    """A naive "citation count == quality" reading would call five sources
    plenty for a clinical claim; `assess_evidence_quality` must cap the
    grade at "low" when none of them carry any date evidence, since the
    clinical profile requires it."""
    profile = DEFAULT_QUALITY_PROFILES["clinical"]
    findings = [_finding(f"https://who.int/{i}", content="undated general text") for i in range(10)]
    result = assess_evidence_quality(findings, profile)
    assert result["usable_sources"] == 10
    assert result["meets_date_requirement"] is False
    assert result["grade"] == "low"


def test_general_profile_with_one_source_and_no_date_requirement_is_not_low_by_default():
    profile = DEFAULT_QUALITY_PROFILES["general"]
    findings = [_finding("https://example.org/x", "some text")]
    result = assess_evidence_quality(findings, profile)
    assert result["meets_min_sources"] is True
    assert result["grade"] in ("medium", "high")


def test_below_minimum_sources_is_low_even_with_dates():
    profile = EvidenceQualityProfile("x", min_sources_per_claim=3, require_date=True)
    findings = [_finding("https://a.example", "2024-01-01 something")]
    result = assess_evidence_quality(findings, profile)
    assert result["meets_min_sources"] is False
    assert result["grade"] == "low"


def test_no_usable_findings_grades_none():
    profile = EvidenceQualityProfile("x", blocked_domains=("spam.example",))
    result = assess_evidence_quality([_finding("https://spam.example/x")], profile)
    assert result["grade"] == "none"
    assert result["usable_sources"] == 0


# ---------------------------------------------------------------------------
# Wiring: DeepResearcher resolves a profile by name and grades its findings.
# ---------------------------------------------------------------------------

def test_unknown_profile_name_falls_back_to_general():
    r = DeepResearcher(llm_endpoint="http://x", llm_model="m", quality_profile="not-a-real-profile")
    assert r.quality_profile.name == "general"


def test_default_profile_is_general_when_omitted():
    r = DeepResearcher(llm_endpoint="http://x", llm_model="m")
    assert r.quality_profile.name == "general"


def test_regression_deep_researcher_grades_findings_after_a_run(monkeypatch):
    """Without `self.evidence_quality` being computed in `research()`, a
    caller asking for RES-06's grade after a run gets nothing at all."""
    r = DeepResearcher(llm_endpoint="http://x", llm_model="m", max_rounds=1, min_rounds=1,
                       max_time=300, quality_profile="clinical")
    pages = [{"url": "https://who.int/a", "title": "T"}]

    async def _search(query):
        return list(pages)
    monkeypatch.setattr(r, "_search", _search)

    async def _extract(url, question, title=""):
        return {"url": url, "title": title, "content": "2024-01-01 clinical finding", "relevance": 0.9}
    monkeypatch.setattr(r, "_fetch_and_extract", _extract)
    monkeypatch.setattr(r, "_create_plan", lambda q: _ready("plan"))
    monkeypatch.setattr(r, "_classify_category", lambda q: _ready(None))
    monkeypatch.setattr(r, "_synthesize", lambda *a, **k: _ready("report"))
    monkeypatch.setattr(r, "_final_report", lambda q, rep: _ready("final: " + rep))

    async def _llm(messages, **k):
        text = messages[-1]["content"]
        if "YES" in text and "NO" in text:
            return "NO"  # stop prompt: keep going (round 1 is also max_rounds here)
        return '["query 1"]'
    monkeypatch.setattr(r, "_llm", _llm)

    asyncio.run(r.research("clinical question"))
    assert r.evidence_quality is not None
    assert r.evidence_quality["profile"] == "clinical"
    assert r.evidence_quality["usable_sources"] == 1


def _ready(value):
    async def _coro(*a, **k):
        return value
    return _coro()
