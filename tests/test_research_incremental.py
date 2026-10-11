"""#274 — incremental Deep Research refresh (section ledger + merge)."""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from src.research_incremental import (
    build_section_ledger,
    citation_numbers,
    evaluate_section,
    merge_updated_sections,
    plan_from_saved,
    plan_refresh,
    refresh_query,
    split_sections,
)


SAMPLE_REPORT = """# Topic

## Background
Early context with citations [1].

## Recent changes
Things that move quickly [2][3].

## Sources
1. old
2. mid
3. new
"""


def test_split_sections_skips_sources_appendix():
    sections = split_sections(SAMPLE_REPORT)
    titles = [s["title"] for s in sections]
    assert titles == ["Background", "Recent changes"]
    assert 1 in citation_numbers(sections[0]["body"])
    assert citation_numbers(sections[1]["body"]) == [2, 3]


def test_build_ledger_ties_sections_to_sources():
    sources = [
        {"number": 1, "url": "https://ex.example/a", "title": "A",
         "fetched_at": "2026-01-01T00:00:00", "evidence": "alpha"},
        {"number": 2, "url": "https://ex.example/b", "title": "B",
         "fetched_at": "2026-10-01T00:00:00", "evidence": "beta"},
        {"number": 3, "url": "https://ex.example/c", "title": "C",
         "fetched_at": "2026-10-10T00:00:00", "evidence": "gamma"},
    ]
    ledger = build_section_ledger(SAMPLE_REPORT, sources=sources)
    assert ledger["section_count"] == 2
    bg = ledger["sections"][0]
    assert bg["title"] == "Background"
    assert bg["citation_numbers"] == [1]
    assert bg["sources"][0]["url"] == "https://ex.example/a"
    assert len(bg["sources"][0]["content_hash"]) == 64


def test_plan_refresh_marks_aged_sections_stale():
    now = datetime(2026, 10, 11, 12, 0, 0)
    old = (now - timedelta(days=30)).isoformat(timespec="seconds")
    fresh = (now - timedelta(days=1)).isoformat(timespec="seconds")
    ledger = {
        "sections": [
            {
                "id": "background-0",
                "title": "Background",
                "sources": [
                    {"url": "https://ex.example/a", "fetched_at": old,
                     "content_hash": "aaa"},
                ],
            },
            {
                "id": "recent-1",
                "title": "Recent changes",
                "sources": [
                    {"url": "https://ex.example/b", "fetched_at": fresh,
                     "content_hash": "bbb"},
                ],
            },
        ]
    }
    plan = plan_refresh(ledger, max_age_days=7, now=now)
    assert plan["redo_count"] == 1
    assert plan["keep_count"] == 1
    assert plan["redo"][0]["title"] == "Background"
    assert plan["keep"][0]["title"] == "Recent changes"
    assert not plan["noop"]


def test_hash_change_marks_section_stale_even_when_young():
    now = datetime(2026, 10, 11, 12, 0, 0)
    young = (now - timedelta(hours=2)).isoformat(timespec="seconds")
    section = {
        "id": "s1",
        "title": "S",
        "sources": [
            {"url": "https://ex.example/x", "fetched_at": young,
             "content_hash": "oldhash"},
        ],
    }
    status = {
        "https://ex.example/x": {"content_hash": "newhash", "changed": True},
    }
    ev = evaluate_section(section, max_age_days=7, now=now, url_status=status)
    assert ev["stale"] is True
    assert any(r.startswith("hash:") for r in ev["reasons"])


def test_merge_updated_sections_stamps_only_redone():
    report = "## Keep\n\nOld keep.\n\n## Redo\n\nOld redo.\n"
    sections = split_sections(report)
    redo_id = next(s["id"] for s in sections if s["title"] == "Redo")
    merged = merge_updated_sections(
        report,
        {redo_id: "Brand new evidence [9]."},
        updated_ids=[redo_id],
    )
    assert "Old keep." in merged
    assert "Brand new evidence [9]." in merged
    assert "Old redo." not in merged
    assert "Updated" in merged


def test_refresh_query_lists_only_stale_titles():
    q = refresh_query(
        "What changed in topic X?",
        {"redo": [{"title": "Recent changes"}, {"title": "Outlook"}]},
    )
    assert "What changed in topic X?" in q
    assert "Recent changes" in q
    assert "Outlook" in q
    assert "ONLY these sections" in q


def test_plan_from_saved_builds_ledger_when_missing():
    saved = {
        "query": "topic",
        "raw_report": SAMPLE_REPORT,
        "result": SAMPLE_REPORT,
        "completed_at": datetime(2020, 1, 1).timestamp(),
        "sources": [
            {"number": 1, "url": "https://ex.example/a", "title": "A",
             "fetched_at": "2020-01-01T00:00:00", "evidence": "a"},
            {"number": 2, "url": "https://ex.example/b", "title": "B",
             "fetched_at": "2020-01-01T00:00:00", "evidence": "b"},
            {"number": 3, "url": "https://ex.example/c", "title": "C",
             "fetched_at": "2020-01-01T00:00:00", "evidence": "c"},
        ],
    }
    plan = plan_from_saved(saved, max_age_days=7, now=datetime(2026, 10, 11))
    assert plan["section_count"] == 2
    assert plan["redo_count"] == 2
    assert plan["complete_refresh"] is True
    assert "Incremental refresh" in plan["refresh_query"]


def test_handler_merge_incremental_by_title():
    from src.research_handler import ResearchHandler
    keep = "## Background\n\nOld bg [1].\n\n## Recent changes\n\nOld recent [2].\n"
    new = "## Background\n\nNEW bg [1].\n\n## Recent changes\n\nStill old [2].\n"
    plan = {
        "redo": [{"id": "x", "title": "Background"}],
        "keep": [{"id": "y", "title": "Recent changes"}],
    }
    merged, meta = ResearchHandler._merge_incremental_report(
        keep_report=keep, new_report=new, plan=plan,
    )
    assert meta["merged"] is True
    assert "NEW bg [1]." in merged
    assert "Old recent [2]." in merged
    assert "Still old" not in merged


def test_start_incremental_refresh_noop_when_fresh(tmp_path, monkeypatch):
    from src import research_handler as rh
    monkeypatch.setattr(rh, "RESEARCH_DATA_DIR", tmp_path)
    monkeypatch.setattr(rh, "DEEP_RESEARCH_DIR", str(tmp_path))

    now = datetime(2026, 10, 11, 12, 0, 0)
    fresh = (now - timedelta(days=1)).isoformat(timespec="seconds")
    report = "## Only\n\nBody [1].\n"
    sid = "rp-freshdemo01"
    import json
    (tmp_path / f"{sid}.json").write_text(json.dumps({
        "query": "q",
        "status": "done",
        "owner": "luis",
        "result": report,
        "raw_report": report,
        "completed_at": now.timestamp(),
        "sources": [
            {"number": 1, "url": "https://ex.example/a", "title": "A",
             "fetched_at": fresh, "evidence": "a"},
        ],
        "section_ledger": build_section_ledger(
            report,
            sources=[{
                "number": 1, "url": "https://ex.example/a", "title": "A",
                "fetched_at": fresh, "evidence": "a",
            }],
            built_at=now,
        ),
    }), encoding="utf-8")

    handler = rh.ResearchHandler()
    started = []

    def fake_start(**kwargs):
        started.append(kwargs)
        return {"session_id": kwargs["session_id"], "status": "running"}

    handler.start_research = fake_start  # type: ignore[method-assign]
    out = handler.start_incremental_refresh(
        sid, "luis",
        resolve_endpoint=lambda _m: ("http://x", "m", {}),
        max_age_days=7,
    )
    assert out["status"] == "noop"
    assert started == []


def test_start_incremental_refresh_seeds_prior_and_skips_fresh_urls(
    tmp_path, monkeypatch,
):
    from src import research_handler as rh
    monkeypatch.setattr(rh, "RESEARCH_DATA_DIR", tmp_path)

    now = datetime(2026, 10, 11, 12, 0, 0)
    old = (now - timedelta(days=40)).isoformat(timespec="seconds")
    fresh = (now - timedelta(days=1)).isoformat(timespec="seconds")
    report = (
        "## Background\n\nOld [1].\n\n"
        "## Recent changes\n\nFresh [2].\n"
    )
    sid = "rp-incrdemo01"
    import json
    sources = [
        {"number": 1, "url": "https://ex.example/old", "title": "Old",
         "fetched_at": old, "evidence": "old"},
        {"number": 2, "url": "https://ex.example/new", "title": "New",
         "fetched_at": fresh, "evidence": "new"},
    ]
    (tmp_path / f"{sid}.json").write_text(json.dumps({
        "query": "topic X",
        "status": "done",
        "owner": "luis",
        "result": report,
        "raw_report": report,
        "completed_at": now.timestamp(),
        "sources": sources,
        "raw_findings": [
            {"url": "https://ex.example/old", "evidence": "old"},
            {"url": "https://ex.example/new", "evidence": "new"},
        ],
        "section_ledger": build_section_ledger(
            report, sources=sources, built_at=now,
        ),
        "max_rounds": 10,
    }), encoding="utf-8")

    handler = rh.ResearchHandler()
    captured = {}

    def fake_start(**kwargs):
        captured.update(kwargs)
        handler._active_tasks[kwargs["session_id"]] = {
            "query": kwargs["query"],
            "status": "running",
        }
        return {"session_id": kwargs["session_id"], "status": "running"}

    handler.start_research = fake_start  # type: ignore[method-assign]
    out = handler.start_incremental_refresh(
        sid, "luis",
        resolve_endpoint=lambda _m: ("http://x", "m", {}),
        max_age_days=7,
        new_session_id="rp-incrnew0001",
    )
    assert out["status"] == "running"
    assert out["incremental_from"] == sid
    assert out["plan"]["redo_count"] == 1
    assert captured["session_id"] == "rp-incrnew0001"
    assert "Incremental refresh" in captured["query"]
    assert "Background" in captured["query"]
    assert captured["prior_report"] == report
    # Fresh URL kept in prior_urls; stale URL omitted so it can be re-fetched.
    assert "https://ex.example/new" in captured["prior_urls"]
    assert "https://ex.example/old" not in captured["prior_urls"]
    entry = handler._active_tasks["rp-incrnew0001"]
    assert entry["incremental_from"] == sid
    assert entry["incremental_keep_report"] == report


def test_real_shape_sources_without_fetched_at_use_registry():
    """Display sources omit fetched_at; registry + completed_at must age sections."""
    now = datetime(2026, 10, 11, 12, 0, 0)
    report = (
        "## Cómo leer este informe\n\nIgnore.\n\n"
        "## Hallazgos\n\nResultado con cita [1].\n\n"
        "## Fuentes\n\n1. x\n"
    )
    saved = {
        "query": "q",
        "raw_report": report,
        "result": report,
        "completed_at": datetime(2026, 9, 26, 18, 0, 0).timestamp(),
        "sources": [
            {"url": "https://ex.example/a", "title": "A", "image": "",
             "citation_verdict": "ok"},  # no fetched_at, no number
        ],
        "citation_registry": {
            "version": 1,
            "sources": [
                {"n": 1, "url": "https://ex.example/a", "title": "A",
                 "fetched_at": "2026-09-26T18:55:09+00:00",
                 "evidence": "alpha", "summary": "s"},
            ],
        },
    }
    plan = plan_from_saved(saved, max_age_days=7, now=now)
    assert plan["redo_count"] >= 1
    assert plan["noop"] is False
    titles = [s["title"] for s in plan["ledger"]["sections"]]
    assert "Cómo leer este informe" not in titles
    assert "Fuentes" not in titles
    assert "Hallazgos" in titles
    linked = plan["ledger"]["sections"][0]["sources"][0]
    assert linked["fetched_at"].startswith("2026-09-26")
    assert linked["url"] == "https://ex.example/a"


def test_citation_registry_n_matches_bracket_numbers():
    report = "## Body\n\nSee [2] then [1].\n"
    sources = [
        {"url": "https://ex.example/first", "title": "First"},  # display order ≠ n
        {"url": "https://ex.example/second", "title": "Second"},
    ]
    registry = {
        "sources": [
            {"n": 1, "url": "https://ex.example/first", "fetched_at": "2026-10-01T00:00:00",
             "evidence": "e1"},
            {"n": 2, "url": "https://ex.example/second", "fetched_at": "2026-10-01T00:00:00",
             "evidence": "e2"},
        ]
    }
    ledger = build_section_ledger(
        report, sources=sources, citation_registry=registry,
        completed_at="2026-10-01T00:00:00",
    )
    by_n = {s["number"]: s["url"] for s in ledger["sections"][0]["sources"]}
    assert by_n[1] == "https://ex.example/first"
    assert by_n[2] == "https://ex.example/second"


def test_section_without_citations_ages_via_built_at():
    now = datetime(2026, 10, 11, 12, 0, 0)
    old = (now - timedelta(days=20)).isoformat(timespec="seconds")
    section = {"id": "intro", "title": "Intro", "sources": [], "built_at": old}
    ev = evaluate_section(section, max_age_days=7, now=now, ledger_built_at=old)
    assert ev["stale"] is True
    assert "age:section" in ev["reasons"]


def test_save_result_writes_section_ledger(tmp_path, monkeypatch):
    from src import research_handler as rh
    monkeypatch.setattr(rh, "RESEARCH_DATA_DIR", tmp_path)

    handler = rh.ResearchHandler()
    sid = "rp-ledgersave01"
    report = "## A\n\nText [1].\n"
    researcher = MagicMock()
    researcher.findings = [
        {"url": "https://ex.example/a", "title": "A", "evidence": "e",
         "fetched_at": "2026-10-10T00:00:00"},
    ]
    researcher.report_language = "en"
    researcher.citations = None
    researcher.stop_reason = None
    researcher.research_trace = None
    researcher.citation_audit = None

    def fake_extract(findings):
        return [{
            "number": 1, "url": "https://ex.example/a", "title": "A",
            "fetched_at": "2026-10-10T00:00:00", "evidence": "e",
        }]

    handler._extract_sources = fake_extract  # type: ignore[method-assign]
    handler._extract_raw_findings = lambda findings: list(findings)  # type: ignore
    handler._attach_citation_verdicts = lambda *a, **k: None  # type: ignore

    entry = {
        "query": "q",
        "status": "done",
        "result": report,
        "raw_report": report,
        "started_at": 1.0,
        "owner": "luis",
        "researcher": researcher,
    }
    handler._save_result(sid, entry)
    import json
    data = json.loads((tmp_path / f"{sid}.json").read_text(encoding="utf-8"))
    assert data["section_ledger"]["section_count"] == 1
    assert data["section_ledger"]["sections"][0]["title"] == "A"
