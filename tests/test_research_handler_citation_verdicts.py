"""Research report citation verdicts (§144 follow-up) — the blind-review
checking pass (`src.research_citations.check_claims`) already settles a
verdict per numbered source, but it was thrown away once the coverage
counts were computed; nothing about it reached `_save_result`'s persisted
"sources", so the Studio report view had nothing to badge each citation
with. `ResearchHandler._attach_citation_verdicts` stamps it back on, matched
by URL against the citation registry (the same identity
`_extract_sources`'s own dedup already keys on) rather than by list
position, since `_extract_sources` can drop/reorder findings the registry
still numbers.
"""
from __future__ import annotations

from types import SimpleNamespace

from src.research_citations import Claim, CheckedClaim, SourceRegistry, VERDICT_REFUTED, VERDICT_SUPPORTED, VERDICT_UNCHECKED
from src.research_handler import ResearchHandler


def _registry(*urls):
    reg = SourceRegistry()
    for i, url in enumerate(urls, 1):
        reg.add({"url": url, "title": f"Source {i}", "summary": f"Summary {i}"})
    return reg


def _checked(number, verdict):
    return CheckedClaim(claim=Claim(text=f"claim [{number}]", numbers=[number], start=0, end=0),
                        number=number, verdict=verdict, layer=None, why="")


def test_attach_citation_verdicts_stamps_each_source_by_url():
    registry = _registry("https://a.test", "https://b.test", "https://c.test")
    researcher = SimpleNamespace(
        citations=registry,
        citation_checked=[_checked(1, VERDICT_SUPPORTED), _checked(2, VERDICT_REFUTED), _checked(3, VERDICT_UNCHECKED)],
    )
    sources = [
        {"url": "https://a.test", "title": "Source 1"},
        {"url": "https://b.test", "title": "Source 2"},
        {"url": "https://c.test", "title": "Source 3"},
    ]
    ResearchHandler._attach_citation_verdicts(sources, researcher)
    by_url = {s["url"]: s for s in sources}
    assert by_url["https://a.test"]["citation_verdict"] == "supported"
    assert by_url["https://b.test"]["citation_verdict"] == "not_supported"
    assert by_url["https://c.test"]["citation_verdict"] == "unverifiable"


def test_attach_citation_verdicts_matches_by_url_not_list_position():
    # _extract_sources can drop/reorder findings (dedup, quality filtering),
    # so a source's position in `sources` need not equal its registry number
    # — only its URL is a shared identity.
    registry = _registry("https://a.test", "https://b.test")
    researcher = SimpleNamespace(
        citations=registry,
        citation_checked=[_checked(1, VERDICT_SUPPORTED), _checked(2, VERDICT_REFUTED)],
    )
    # b.test (registry number 2) listed FIRST in `sources`.
    sources = [{"url": "https://b.test", "title": "B"}, {"url": "https://a.test", "title": "A"}]
    ResearchHandler._attach_citation_verdicts(sources, researcher)
    assert sources[0]["citation_verdict"] == "not_supported"
    assert sources[1]["citation_verdict"] == "supported"


def test_attach_citation_verdicts_leaves_unmatched_sources_alone():
    # A source the checking pass never had a verdict for (nothing cited it
    # with a checkable figure) gets no key at all — undefined on the wire,
    # never a guessed "unverifiable".
    registry = _registry("https://a.test", "https://b.test")
    researcher = SimpleNamespace(citations=registry, citation_checked=[_checked(1, VERDICT_SUPPORTED)])
    sources = [{"url": "https://a.test"}, {"url": "https://b.test"}, {"url": "https://not-registered.test"}]
    ResearchHandler._attach_citation_verdicts(sources, researcher)
    assert sources[0]["citation_verdict"] == "supported"
    assert "citation_verdict" not in sources[1]
    assert "citation_verdict" not in sources[2]


def test_attach_citation_verdicts_no_checking_pass_is_a_no_op():
    registry = _registry("https://a.test")
    researcher = SimpleNamespace(citations=registry, citation_checked=[])
    sources = [{"url": "https://a.test"}]
    ResearchHandler._attach_citation_verdicts(sources, researcher)
    assert "citation_verdict" not in sources[0]


def test_attach_citation_verdicts_handles_missing_researcher_and_empty_sources():
    # Best-effort like every other optional field _save_result attaches —
    # never raises for a None researcher, no registry, or nothing to stamp.
    ResearchHandler._attach_citation_verdicts([], None)
    ResearchHandler._attach_citation_verdicts([{"url": "https://a.test"}], None)
    ResearchHandler._attach_citation_verdicts([], SimpleNamespace(citations=_registry("https://a.test"), citation_checked=[]))
    researcher_no_registry = SimpleNamespace(citations=None, citation_checked=[_checked(1, VERDICT_SUPPORTED)])
    sources = [{"url": "https://a.test"}]
    ResearchHandler._attach_citation_verdicts(sources, researcher_no_registry)
    assert "citation_verdict" not in sources[0]
