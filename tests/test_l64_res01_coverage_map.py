"""Lote 64 — RES-01 backend half: per-node coverage status (pending/covered/
insufficient) for the research schema (`self.subquestions`), exposed in the
run's progress state so the UI can paint it (studio wiring is another lote).
"""
import asyncio

from unittest.mock import AsyncMock

from src.deep_research import DeepResearcher


def _finding(url, title, evidence):
    return {"url": url, "title": title, "summary": "", "evidence": evidence}


def test_a_node_with_no_matching_finding_is_pending():
    r = DeepResearcher("http://unused.invalid", "m")
    r.subquestions = ["What are the long-term effects of whiplash?",
                       "Which physiotherapy protocols are recommended?"]
    findings = [_finding("https://a.test/1", "Whiplash outcomes",
                         "Long-term effects of whiplash injury are well documented")]

    snap = r._coverage_snapshot(findings)

    assert snap[0]["status"] in ("insufficient", "covered")  # matched by "whiplash"/"effects"
    assert snap[1]["status"] == "pending"  # physiotherapy never mentioned


def test_a_node_with_one_source_is_insufficient_and_two_sources_is_covered():
    r = DeepResearcher("http://unused.invalid", "m")
    r.subquestions = ["What physiotherapy protocols treat whiplash?"]

    one_source = [_finding("https://a.test/1", "Whiplash physiotherapy",
                           "physiotherapy protocols for whiplash recovery")]
    snap_one = r._coverage_snapshot(one_source)
    assert snap_one[0]["status"] == "insufficient"
    assert snap_one[0]["matched_sources"] == 1

    two_sources = one_source + [
        _finding("https://b.test/2", "Another whiplash physiotherapy study",
                 "a second study on physiotherapy protocols for whiplash patients")]
    snap_two = r._coverage_snapshot(two_sources)
    assert snap_two[0]["status"] == "covered"
    assert snap_two[0]["matched_sources"] == 2


def test_the_same_url_cited_twice_counts_once_not_covered():
    r = DeepResearcher("http://unused.invalid", "m")
    r.subquestions = ["What is the recommended dosage of ibuprofen?"]
    findings = [
        _finding("https://a.test/1", "Ibuprofen dosage", "ibuprofen dosage guidelines"),
        _finding("https://a.test/1", "Ibuprofen dosage (re-read)", "ibuprofen dosage guidelines again"),
    ]
    snap = r._coverage_snapshot(findings)
    assert snap[0]["status"] == "insufficient"
    assert snap[0]["matched_sources"] == 1


def test_an_empty_schema_returns_an_empty_map_not_an_error():
    r = DeepResearcher("http://unused.invalid", "m")
    r.subquestions = []
    assert r._coverage_snapshot([]) == []


def test_a_malformed_finding_is_skipped_not_raised():
    r = DeepResearcher("http://unused.invalid", "m")
    r.subquestions = ["What is the treatment?"]
    findings = [None, "not a dict", {"url": "https://a.test/1", "summary": "treatment options"}]
    snap = r._coverage_snapshot(findings)
    assert snap[0]["status"] == "insufficient"


def test_coverage_reaches_the_progress_events_research_emits():
    """The wiring RES-01 asked for: a poller reading progress events (what
    `research_handler.ResearchHandler.get_status` surfaces as `progress`)
    sees a `coverage` list on the per-round "analyzing" event."""
    r = DeepResearcher("http://unused.invalid", "m", max_rounds=1, min_rounds=1)
    r._create_plan = AsyncMock(return_value="plan")
    r._classify_category = AsyncMock(return_value="")
    r._generate_queries = AsyncMock(return_value=["whiplash treatment"])
    r._search_and_extract = AsyncMock(return_value=[
        _finding("https://a.test/1", "Whiplash treatment guide",
                 "an overview of whiplash treatment options and outcomes")])
    r._synthesize = AsyncMock(return_value="report")
    r._should_stop = AsyncMock(return_value=True)
    r.subquestions = ["What treatment options exist for whiplash?"]

    events = []
    r._progress = events.append

    asyncio.run(r.research("What treatment options exist for whiplash?"))

    analyzing = [e for e in events if e.get("phase") == "analyzing"]
    assert analyzing, "expected at least one 'analyzing' progress event"
    coverage = analyzing[-1].get("coverage")
    assert coverage and coverage[0]["question"] == "What treatment options exist for whiplash?"
    assert coverage[0]["status"] in ("insufficient", "covered")
