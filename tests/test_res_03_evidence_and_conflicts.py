"""RES-03 — sources carry a full EvidenceRef (URL, content hash, capture
date, the engine that found them), dedup by canonical URL, and disagreeing
sources are surfaced in the report as a "sources in disagreement" section --
never silently resolved.
"""
from src.deep_research import DeepResearcher
from src.research_citations import (
    SourceRegistry,
    finalize_report,
    find_conflicts,
)


def _finding(url, summary, **extra):
    return {"url": url, "title": extra.pop("title", "T"), "summary": summary, **extra}


def test_a_source_carries_its_evidenceref_fields():
    reg = SourceRegistry()
    n = reg.add(_finding("https://a.example.com/page", "some real content here " * 3,
                         engine="searxng", fetched_at="2026-09-10T00:00:00+00:00"))
    entry = reg.source(n)
    assert entry["url"] == "https://a.example.com/page"
    assert entry["fetched_at"] == "2026-09-10T00:00:00+00:00"
    assert reg.engine_of(n) == "searxng"
    assert reg.content_hash_of(n)  # a real hash was computed for real content
    assert len(reg.content_hash_of(n)) == 64  # sha256 hex digest


def test_dedup_by_canonical_url_keeps_one_number_and_fills_gaps():
    reg = SourceRegistry()
    n1 = reg.add({"url": "https://a.example.com/x?utm_source=t", "title": "",
                  "summary": "", "engine": "searxng"})
    n2 = reg.add({"url": "https://a.example.com/x", "title": "Real title",
                  "summary": "Real summary", "engine": "brave"})
    assert n1 == n2
    entry = reg.source(n1)
    assert entry["title"] == "Real title"  # the empty field got filled in
    assert reg.engine_of(n1) == "searxng"  # first engine recorded wins


def test_duplicate_candidates_flags_identical_content_across_urls_without_merging():
    """Content-hash dedup is DETECTION, not automatic merging -- two distinct
    URLs whose extracted text is identical are flagged as likely mirrors, but
    keep separate citation numbers (a real source's own field-set contract is
    covered by tests/test_research_citations.py, which this must not break)."""
    reg = SourceRegistry()
    body = "This exact paragraph appears on both the origin site and a mirror. " * 3
    n1 = reg.add({"url": "https://origin.example.com/article", "summary": body})
    n2 = reg.add({"url": "https://mirror.example.com/article-copy", "summary": body})
    assert n1 != n2  # NOT auto-merged
    groups = reg.duplicate_candidates()
    assert any(set(g) == {n1, n2} for g in groups)


def test_engine_and_content_hash_survive_a_snapshot_restore_round_trip():
    reg = SourceRegistry()
    n = reg.add({"url": "https://a.example.com/p", "summary": "durable content " * 5,
                "engine": "firecrawl"})
    original_hash = reg.content_hash_of(n)
    restored = SourceRegistry.restore(reg.snapshot())
    assert restored.engine_of(n) == "firecrawl"
    assert restored.content_hash_of(n) == original_hash


def test_an_old_snapshot_without_engine_still_restores():
    """Backward compatibility: a snapshot written before RES-03 has no
    "engine" key at all. `restore()` must not reject it."""
    old_snapshot = {
        "version": 1,
        "sources": [{
            "n": 1, "url": "https://a.example.com/p", "title": "T",
            "summary": "s", "evidence": "e", "fetched_at": "2026-01-01T00:00:00+00:00",
        }],
    }
    restored = SourceRegistry.restore(old_snapshot)
    assert len(restored) == 1
    assert restored.engine_of(1) == ""


def test_two_sources_that_disagree_are_named_not_silently_resolved():
    reg = SourceRegistry()
    reg.add(_finding("https://a.example.com/x",
                     "Recovery time after whiplash is usually 6 weeks according to clinicians surveyed."))
    reg.add(_finding("https://b.example.com/y",
                     "Recovery time after whiplash is usually 12 weeks according to clinicians surveyed."))
    conflicts = find_conflicts(reg)
    assert len(conflicts) == 1
    values = {row[1] for row in conflicts[0].entries}
    assert values == {"6 weeks", "12 weeks"}


def test_agreeing_sources_are_not_reported_as_conflicts():
    reg = SourceRegistry()
    reg.add(_finding("https://a.example.com/x",
                     "Recovery time after whiplash is usually 6 weeks according to clinicians surveyed."))
    reg.add(_finding("https://b.example.com/y",
                     "Recovery time after whiplash is usually 6 weeks according to clinicians surveyed."))
    assert find_conflicts(reg) == []


def test_finalize_report_prints_a_disagreement_section_for_cited_sources():
    reg = SourceRegistry()
    reg.add(_finding("https://a.example.com/x",
                     "Recovery time after whiplash is usually 6 weeks according to clinicians surveyed."))
    reg.add(_finding("https://b.example.com/y",
                     "Recovery time after whiplash is usually 12 weeks according to clinicians surveyed."))
    report = "Whiplash recovery varies. [1] Some clinics report longer courses. [2]\n"

    final, audit, _checked = finalize_report(report, reg, language="es")

    assert "Desacuerdos entre fuentes" in final
    assert "6 weeks" in final and "12 weeks" in final
    assert audit.coverage.get("conflicts") == 1


def test_an_ungathered_or_uncited_source_disagreement_is_not_reported():
    """Only sources the report actually cites are checked for conflicts -- an
    uncited source's own contradiction with another is not the reader's
    problem (research_citations.find_conflicts, `only_sources`)."""
    reg = SourceRegistry()
    reg.add(_finding("https://a.example.com/x",
                     "Recovery time after whiplash is usually 6 weeks according to clinicians surveyed."))
    reg.add(_finding("https://b.example.com/y",
                     "Recovery time after whiplash is usually 12 weeks according to clinicians surveyed."))
    report = "Whiplash recovery varies. [1]\n"  # only cites source 1

    final, audit, _checked = finalize_report(report, reg, language="en")

    assert "Disagreements between sources" not in final
    assert audit.coverage.get("conflicts", 0) == 0


def test_finalize_report_with_conflicts_is_idempotent():
    reg = SourceRegistry()
    reg.add(_finding("https://a.example.com/x",
                     "Recovery time after whiplash is usually 6 weeks according to clinicians surveyed."))
    reg.add(_finding("https://b.example.com/y",
                     "Recovery time after whiplash is usually 12 weeks according to clinicians surveyed."))
    report = "Whiplash recovery varies. [1] Some clinics disagree. [2]\n"

    final1, _audit1, _c1 = finalize_report(report, reg, language="en")
    final2, _audit2, _c2 = finalize_report(final1, reg, language="en")
    assert final1 == final2


def test_engine_is_recorded_end_to_end_from_search_result_to_source(monkeypatch):
    """`_search` tags results with the provider that returned them, and that
    provenance survives through extraction into the SourceRegistry entry --
    without changing `_fetch_and_extract`'s call signature, since other
    tests (e.g. tests/test_deep_research_same_pages_again.py) replace that
    method wholesale with a narrower mock and must keep working."""
    import asyncio
    import sys
    import types

    r = DeepResearcher.__new__(DeepResearcher)
    r.search_provider_override = None
    r.providers_used = []
    r.urls_fetched = set()
    r.analyzed_urls = []
    r._url_engine = {}
    r.max_urls_per_round = 5
    r.extraction_concurrency = 2
    r._engine_warned = True  # skip the searxng-health side check
    r._last_round_note = ""
    r._cancelled = False
    import time as _time
    r._start_time = _time.time()
    r.max_time = 10**9

    providers_mod = types.ModuleType("src.search.providers")
    providers_mod._get_search_settings = lambda: {"search_provider": "searxng"}
    core_mod = types.ModuleType("src.search.core")
    core_mod._build_provider_chain = lambda provider: ["searxng"]
    core_mod._call_provider = lambda prov, query, n: [{"url": "https://a.example.com/found", "title": "Found"}]
    monkeypatch.setitem(sys.modules, "src.search.providers", providers_mod)
    monkeypatch.setitem(sys.modules, "src.search.core", core_mod)

    async def fake_extract(url, question, title):
        # Real `_fetch_and_extract` looks up `self._url_engine[url]` itself;
        # this stand-in just proves that lookup was populated before it ran.
        assert r._url_engine.get(url) == "searxng"
        return {"url": url, "title": title, "summary": "extracted content", "engine": r._url_engine.get(url)}

    r._fetch_and_extract = fake_extract

    findings = asyncio.run(r._search_and_extract(["query"], "question"))
    assert findings and findings[0]["engine"] == "searxng"

    reg = SourceRegistry()
    reg.add(findings[0])
    assert reg.engine_of(1) == "searxng"
