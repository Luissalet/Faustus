import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.research_citations import SourceRegistry


def registry():
    result = SourceRegistry()
    for letter in "abc":
        result.add({"url": f"https://example.test/{letter}", "title": letter,
                    "summary": "Evidence collected from the primary source.", "evidence": "42 units"})
    return result


def test_registry_roundtrip_keeps_numbers_after_display_reorder_and_filter():
    old = registry()
    restored = SourceRegistry.restore(json.loads(json.dumps(old.snapshot())))
    for row in [old.source(3), old.source(1)]:
        assert restored.add(row) == row["n"]
    assert restored.source(2)["url"].endswith("/b")
    assert restored.add({"url": "https://example.test/new"}) == 4
    assert old.snapshot() == SourceRegistry.restore(old.snapshot()).snapshot()


@pytest.mark.parametrize("kind", ["version", "number", "bool", "duplicate", "url", "text"])
def test_corrupted_snapshot_never_silently_renumbers(kind):
    snapshot = copy.deepcopy(registry().snapshot())
    if kind == "version":
        snapshot["version"] = True
    elif kind == "number":
        snapshot["sources"][1]["n"] = 3
    elif kind == "bool":
        snapshot["sources"][0]["n"] = True
    elif kind == "duplicate":
        snapshot["sources"][1]["url"] = snapshot["sources"][0]["url"] + "?utm_source=test"
    elif kind == "url":
        snapshot["sources"][1]["url"] = "javascript:alert(1)"
    else:
        snapshot["sources"][1]["summary"] = 7
    with pytest.raises(ValueError):
        SourceRegistry.restore(snapshot)


def test_snapshot_text_is_bounded_without_dropping_identities():
    old = SourceRegistry()
    for number in range(300):
        old.add({"url": f"https://example.test/{number}", "evidence": "x" * 40000})
    snapshot = old.snapshot()
    assert sum(len(row["evidence"]) for row in snapshot["sources"]) <= 8 * 1024 * 1024
    assert len(SourceRegistry.restore(snapshot)) == 300


def test_real_continuation_uses_saved_registry_before_adding_findings(monkeypatch):
    from src.deep_research import DeepResearcher
    old = registry()
    researcher = DeepResearcher("http://unused.invalid", "unused", max_rounds=0)
    monkeypatch.setattr(researcher, "_create_plan", AsyncMock(return_value="plan"))
    monkeypatch.setattr(researcher, "_final_report", AsyncMock(return_value="Finding [2]."))
    asyncio.run(researcher.research("Continue", prior_report="Finding [2].",
                                   prior_findings=[old.source(3), old.source(1)],
                                   prior_citations=old.snapshot()))
    assert researcher.citations.number_for("https://example.test/b") == 2
    assert researcher.citations.number_for("https://example.test/c") == 3


def test_saved_research_persists_filtered_out_sources_atomically(tmp_path, monkeypatch):
    from src import research_handler
    handler = research_handler.ResearchHandler.__new__(research_handler.ResearchHandler)
    monkeypatch.setattr(research_handler, "RESEARCH_DATA_DIR", tmp_path)
    old = registry()
    entry = {"query": "test", "status": "done", "result": "Finding [2].",
             "started_at": 1, "owner": "alice",
             "researcher": SimpleNamespace(citations=old, findings=[old.source(3)])}
    handler._save_result("rp-citations", entry)
    saved = handler._get_session_json("rp-citations")
    assert saved["owner"] == "alice"
    assert SourceRegistry.restore(saved["citation_registry"]).source(2)["url"].endswith("/b")
    assert len(list(tmp_path.iterdir())) == 1
    assert handler._get_session_json("rp-citations", owner="alice") is not None
    assert handler._get_session_json("rp-citations", owner="bob") is None
    assert handler._get_session_json("rp-citations", owner="") is None


def test_research_result_keeps_previous_file_on_failed_atomic_replace(tmp_path, monkeypatch):
    from src import research_handler
    from core import atomic_io
    handler = research_handler.ResearchHandler.__new__(research_handler.ResearchHandler)
    monkeypatch.setattr(research_handler, "RESEARCH_DATA_DIR", tmp_path)
    target = tmp_path / "rp-preserved.json"
    target.write_text('{"result":"previous"}', encoding="utf-8")
    def refuse(*args):
        raise PermissionError("held by reader")
    monkeypatch.setattr(atomic_io.os, "replace", refuse)
    handler._save_result("rp-preserved", {"query": "test", "status": "done", "result": "new", "started_at": 1})
    assert json.loads(target.read_text(encoding="utf-8"))["result"] == "previous"
    assert len(list(tmp_path.iterdir())) == 1


def test_bad_snapshot_is_rejected_before_provider_or_fallback(monkeypatch):
    from src.research_handler import ResearchHandler
    handler = ResearchHandler.__new__(ResearchHandler)
    probe = AsyncMock()
    fallback = AsyncMock()
    monkeypatch.setattr(handler, "_probe_endpoint", probe)
    monkeypatch.setattr(handler, "_fallback_research", fallback)
    with pytest.raises(ValueError):
        asyncio.run(handler.call_research_service("query", "http://unused.invalid", "unused",
                                                 prior_citations={"version": 55}))
    probe.assert_not_called()
    fallback.assert_not_called()


@pytest.mark.parametrize("url", ["javascript:alert(1)", "file:///secret", "https://user:secret@example.test/",
                               "https://example.test:invalid/", "https://[broken", "https://example.test/" + "x" * 8192])
def test_source_registry_rejects_unsafe_or_malformed_urls(url):
    assert SourceRegistry().add({"url": url}) == 0
