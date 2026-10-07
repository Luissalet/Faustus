"""OBJ-25: the report view can only show what reaches it.

Two gaps stood between the checks a research run already makes and the
reader of its report:

- `per_source_verdicts` reduces a source to ONE word and lets `supported`
  win, so a source that backs one sentence and contradicts another read as
  plain "supported", with no sentence and no reason anywhere. The handler now
  keeps, per source, the counts and the citing sentences with the checker's
  reason (`citation_check_counts`, `citation_checks`).
- §144's blind review was saved next to the result but `/result` and
  `/result-peek` (what the Research screen reads) never returned it.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from routes.research_routes import setup_research_routes
from src.research_citations import (
    CheckedClaim, Claim, SourceRegistry, VERDICT_REFUTED, VERDICT_SUPPORTED, VERDICT_UNCHECKED,
)
from src.research_handler import ResearchHandler


def _checked(number, verdict, text, why=""):
    return CheckedClaim(claim=Claim(text=text, numbers=[number], start=0, end=0),
                        number=number, verdict=verdict, layer=4, why=why)


def _registry(*urls):
    reg = SourceRegistry()
    for i, url in enumerate(urls, 1):
        reg.add({"url": url, "title": f"Source {i}", "summary": f"Summary {i}"})
    return reg


def test_a_source_that_backs_one_sentence_and_not_another_keeps_both():
    checked = [
        _checked(1, VERDICT_SUPPORTED, "Recovery takes 12 weeks [1].", "every figure occurs in the source"),
        _checked(1, VERDICT_REFUTED, "Half of patients, 48 %, still hurt at a year [1].",
                 "the source does not contain 48"),
        _checked(2, VERDICT_UNCHECKED, "Exercise helps [2].", "no figures"),
    ]
    researcher = SimpleNamespace(citations=_registry("https://a.test", "https://b.test"), citation_checked=checked)
    sources = [{"url": "https://a.test"}, {"url": "https://b.test"}]
    ResearchHandler._attach_citation_verdicts(sources, researcher)

    a, b = sources
    # The one-word badge is unchanged (supported wins, as before) ...
    assert a["citation_verdict"] == "supported"
    # ... but the counts no longer hide the failed sentence.
    assert a["citation_check_counts"] == {"supported": 1, "not_supported": 1, "unverifiable": 0}
    # Worst first, markers stripped, reason kept.
    assert [c["verdict"] for c in a["citation_checks"]] == ["not_supported", "supported"]
    assert a["citation_checks"][0]["sentence"] == "Half of patients, 48 %, still hurt at a year."
    assert a["citation_checks"][0]["why"] == "the source does not contain 48"
    assert a["citation_checks"][0]["layer"] == 4
    assert b["citation_verdict"] == "unverifiable"
    assert b["citation_check_counts"] == {"supported": 0, "not_supported": 0, "unverifiable": 1}


def test_the_sentence_list_is_capped_but_the_counts_are_not():
    many = [_checked(1, VERDICT_SUPPORTED, f"Claim {i} costs {i} euros [1].") for i in range(30)]
    detail = ResearchHandler._citation_checks_by_number(many)
    assert detail[1]["counts"]["supported"] == 30
    assert len(detail[1]["checks"]) == ResearchHandler.CITATION_CHECKS_PER_SOURCE


def test_the_catch_all_source_zero_is_never_a_source():
    claim = CheckedClaim(claim=Claim(text="No citation here.", numbers=[], start=0, end=0),
                         number=0, verdict=VERDICT_UNCHECKED, layer=None, why="no resolvable citation")
    assert ResearchHandler._citation_checks_by_number([claim]) == {}


def test_an_unchecked_source_gets_no_detail_either():
    researcher = SimpleNamespace(citations=_registry("https://a.test", "https://b.test"),
                                 citation_checked=[_checked(1, VERDICT_SUPPORTED, "It is 3 m long [1].")])
    sources = [{"url": "https://a.test"}, {"url": "https://b.test"}]
    ResearchHandler._attach_citation_verdicts(sources, researcher)
    assert "citation_checks" not in sources[1]
    assert "citation_check_counts" not in sources[1]


# ── routes ────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _redirect_research_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("routes.research_routes.DEEP_RESEARCH_DIR", str(tmp_path / "data" / "deep_research"))


def _request(user):
    return SimpleNamespace(state=SimpleNamespace(current_user=user))


def _route(router, path, method):
    for route in router.routes:
        if getattr(route, "path", "") == path and method in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(f"{method} {path} route not registered")


def _handler():
    handler = MagicMock()
    handler._active_tasks = {}
    handler.get_result.return_value = None
    return handler


REVIEW = {"model": "reviewer-3b", "overall": 2, "scores": {"answers_question": 3}, "weaknesses": ["thin"],
          "unsupported_claims": ["48 % still hurt"], "calibration_gap": 0.42, "duration_s": 7.1}


def _write(tmp_path, sid, **data):
    d = tmp_path / "data" / "deep_research"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{sid}.json").write_text(json.dumps(data), encoding="utf-8")


def test_peek_of_a_saved_report_returns_its_blind_review(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, "rp-review", owner="alice", status="done", result="report", sources=[], blind_review=REVIEW)
    peek = _route(setup_research_routes(_handler()), "/api/research/result-peek/{session_id}", "POST")
    out = asyncio.run(peek(session_id="rp-review", request=_request("alice")))
    assert out["blind_review"] == REVIEW


def test_a_failed_review_travels_too_and_a_missing_one_is_not_invented(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, "rp-failed", owner="alice", status="done", result="r",
           blind_review={"model": "m", "overall": None, "error": "blind review timed out after 135s"})
    _write(tmp_path, "rp-none", owner="alice", status="done", result="r", blind_review=None)
    peek = _route(setup_research_routes(_handler()), "/api/research/result-peek/{session_id}", "POST")
    failed = asyncio.run(peek(session_id="rp-failed", request=_request("alice")))
    assert failed["blind_review"]["error"].startswith("blind review timed out")
    none = asyncio.run(peek(session_id="rp-none", request=_request("alice")))
    assert "blind_review" not in none


def test_result_of_a_run_in_memory_returns_its_blind_review(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    handler = _handler()
    handler._active_tasks = {"rp-live": {"owner": "alice", "status": "done", "blind_review": REVIEW}}
    handler.get_blind_review.side_effect = lambda sid: REVIEW if sid == "rp-live" else None
    handler.get_result.return_value = "report"
    handler.get_sources.return_value = []
    handler.get_raw_findings.return_value = []
    result = _route(setup_research_routes(handler), "/api/research/result/{session_id}", "POST")
    out = asyncio.run(result(session_id="rp-live", request=_request("alice")))
    assert out["blind_review"] == REVIEW


def test_the_handler_reads_the_review_from_memory_then_from_disk(tmp_path, monkeypatch):
    # A saved result that was never consumed is answered by `/result` from
    # disk (`get_result` reads the JSON), with no in-memory entry: the review
    # must come from the same file, or a reloaded screen never sees it.
    import src.research_handler as rh
    monkeypatch.setattr(rh, "_research_json_path", lambda sid: tmp_path / f"{sid}.json")
    handler = ResearchHandler.__new__(ResearchHandler)
    handler._active_tasks = {"live": {"status": "done", "blind_review": REVIEW}, "off": {"status": "done"}}
    (tmp_path / "saved.json").write_text(json.dumps({"blind_review": {"model": "m", "error": "timed out"}}), encoding="utf-8")
    (tmp_path / "off.json").write_text(json.dumps({"blind_review": None}), encoding="utf-8")
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    assert handler.get_blind_review("live") == REVIEW
    assert handler.get_blind_review("saved") == {"model": "m", "error": "timed out"}
    assert handler.get_blind_review("off") is None
    assert handler.get_blind_review("broken") is None
    assert handler.get_blind_review("missing") is None
