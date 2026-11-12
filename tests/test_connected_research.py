import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src import connected_research as cr
from src import research_handler as rh
from src.research_citations import audit_citations


def researcher(**kwargs):
    r = cr.ConnectedResearcher(llm_endpoint="http://unused/v1/chat/completions", llm_model="test", owner="alice", **kwargs)
    r.report_language = "es"
    return r


def tool(source="mcp:links", name="search_links", modes=None):
    props = {"query": {"type": "string"}, "tag": {"type": "string"}}
    if modes:
        props["mode"] = {"type": "string", "enum": modes}
    return {"source": source, "name": name, "qualified_name": "mcp__links__" + name,
            "query_key": "query", "input_schema": {"properties": props, "required": ["query"]},
            "connector": {"preset_id": "links", "ui_url": "http://links.test"}}


@pytest.mark.parametrize("result", [
    {"stdout": '{"results":[{"url":"https://source.test","text":"Evidence"}]}', "exit_code": 0},
    {"structured_content": {"results": [{"url": "https://source.test", "text": "Evidence"}]}},
    {"content": [{"type": "text", "text": '{"items":[{"url":"https://source.test","text":"Evidence"}]}'}]},
])
def test_real_mcp_envelopes_are_unwrapped(result):
    assert cr._rows(result)[0]["text"] == "Evidence"


@pytest.mark.parametrize("url", ["javascript:alert(1)", "data:text/html,secret", "//evil.test", "", "file:///private"])
def test_uncitable_urls_are_excluded(url):
    assert cr._finding({"url": url, "text": "Evidence"}, "links") is None


def test_three_sources_two_lanes_filters_rrf_and_originals(monkeypatch):
    r = researcher(source_ids=["notes", "mcp:links", "mcp:kafka"], source_filters={"mcp:links": {"tag": "project"}})
    r.manager = SimpleNamespace(call_tool=AsyncMock(side_effect=lambda name, args: {"stdout": json.dumps({"items": [
        {"url": "/item/" + ("links" if name.endswith("search_links") else "kafka"), "title": "Original", "content": "The project is due on Friday."}
    ]}), "exit_code": 0}))
    monkeypatch.setattr(cr, "_tools", lambda *a: [tool(modes=["text", "semantic"]), tool("mcp:kafka", "search_documents")])
    r._local = AsyncMock(return_value=[{"url": "http://faustus.test/notes?n=3", "content": "Friday is confirmed."}])
    findings = asyncio.run(r._search_and_extract(["project"], "When is the project due?"))
    assert len(findings) == 3
    assert {f["source_id"] for f in findings} == {"notes", "mcp:links", "mcp:kafka"}
    calls = r.manager.call_tool.call_args_list
    assert {c.args[1].get("mode") for c in calls if c.args[0].endswith("search_links")} == {"text", "semantic"}
    assert all(c.args[1]["tag"] == "project" for c in calls if c.args[0].endswith("search_links"))
    assert any(e["status"] == "running" for e in r.timeline)
    assert len([e for e in r.timeline if e["status"] == "done" and e["lane"] != "ranking"]) == 4
    assert asyncio.run(r._search_and_extract(["project"], "question")) == []


def test_source_failure_and_unsupported_filter_are_visible(monkeypatch):
    r = researcher(source_ids=["mcp:links", "mcp:missing"], source_filters={"mcp:links": {"unsupported": True}})
    r.manager = SimpleNamespace(call_tool=AsyncMock())
    monkeypatch.setattr(cr, "_tools", lambda *a: [tool()])
    with pytest.raises(cr.ResearchFailed, match="no citable evidence"):
        asyncio.run(r._search_and_extract(["question"], "question"))
    assert len([e for e in r.timeline if e["status"] == "error"]) == 2
    r.manager.call_tool.assert_not_called()


def test_uncited_synthesis_is_replaced_by_cited_evidence():
    r = researcher()
    evidence = "The recorded deadline is Friday. " + "The owner confirmed the date. " * 100 + "Unneeded final paragraph."
    r.citations.add({"url": "https://source.test/a", "title": "Original", "evidence": evidence})
    report = r._finalize_citations("The deadline is Monday and the budget is 1000 euros.")
    assert "Monday" not in report and "Friday" in report
    assert "Unneeded final paragraph" not in report
    assert audit_citations(report, r.citations).used == [1]
    assert r.citation_audit.cited_sentences == r.citation_audit.total_sentences


def test_private_mode_never_starts_web_backend_or_web_fallback(monkeypatch):
    h = rh.ResearchHandler.__new__(rh.ResearchHandler)
    h._probe_endpoint = AsyncMock()
    h._ensure_search_backend = AsyncMock()
    h._fallback_research = AsyncMock()
    monkeypatch.setattr(cr, "ConnectedResearcher", lambda **kw: SimpleNamespace(research=AsyncMock(side_effect=RuntimeError("source down"))))
    with pytest.raises(RuntimeError, match="source down"):
        asyncio.run(h.call_research_service("question", "http://unused", "test", connected_options={"source_ids": ["notes"]}))
    h._ensure_search_backend.assert_not_called()
    h._fallback_research.assert_not_called()


def test_marker_and_finished_report_preserve_connected_options(tmp_path, monkeypatch):
    monkeypatch.setattr(rh, "RESEARCH_DATA_DIR", tmp_path)
    h = rh.ResearchHandler.__new__(rh.ResearchHandler)
    h._active_tasks = {}
    options = {"source_ids": ["notes"], "source_filters": {}, "clarification": "this week"}
    progress = {"data_scope": "connected", "plan": "Read notes", "timeline": [{"id": 1}]}
    entry = {"query": "question", "status": "running", "owner": "alice", "started_at": 1,
             "connected_options": options, "connected_progress": progress}
    h._write_marker("rp-own", entry)
    h._write_checkpoint("rp-own", {"rounds_done": 1})
    assert h._get_session_json("rp-own", owner="alice")["connected_options"] == options
    entry.update(status="done", result="Evidence", researcher=None)
    h._save_result("rp-own", entry)
    assert h._get_session_json("rp-own", owner="alice")["connected_progress"] == progress
    assert h._get_session_json("rp-own", owner="bob") is None


def test_catalog_excludes_other_owners_disabled_and_write_tools(monkeypatch):
    import sys
    from core import database
    from src import connector_sidecar
    monkeypatch.setitem(sys.modules, "src.database", database)
    query = SimpleNamespace(filter=lambda *a: query, all=lambda: [SimpleNamespace(id="shared"), SimpleNamespace(id="foreign")])
    monkeypatch.setattr(database, "SessionLocal", lambda: SimpleNamespace(query=lambda *a: query, close=lambda: None))
    monkeypatch.setattr(connector_sidecar, "list_connectors", lambda: [{"server_id": "foreign", "owner": "bob"}])
    def row(name, server="shared", **extra):
        return {"server_id": server, "server_name": server, "name": name, "qualified_name": "mcp__" + server + "__" + name,
                "input_schema": {"properties": {"query": {"type": "string"}}}, **extra}
    manager = SimpleNamespace(is_builtin=lambda *a: False, get_all_tools=lambda: [row("search_notes"), row("search_foreign", "foreign"),
        row("search_disabled", is_disabled=True), row("search_and_delete", annotations={"readOnlyHint": False})])
    assert [t["name"] for t in cr._tools("alice", manager)] == ["search_notes"]


def test_project_original_rejects_foreign_project(monkeypatch):
    from services import projects
    monkeypatch.setattr(projects, "get_store", lambda: SimpleNamespace(get=lambda *a: None))
    with pytest.raises(FileNotFoundError):
        cr.read_project_source("bob", "alice-project", "private")


def test_saved_link_is_read_and_cites_the_stored_reader():
    r = researcher()
    search = tool()
    search["server_id"] = "links"
    reader = {"server_id": "links", "name": "read_link", "qualified_name": "mcp__links__read_link",
              "annotations": {"readOnlyHint": True}}
    r.manager = SimpleNamespace(get_all_tools=lambda: [reader], call_tool=AsyncMock(side_effect=[
        {"stdout": json.dumps({"items": [{"id": "saved-12", "url": "https://publisher.test/article", "snippet": "Search excerpt"}]})},
        {"stdout": json.dumps({"text": "The actual saved article text says the deadline is Friday."})},
    ]))
    rows = asyncio.run(r._connector(search, "deadline", "text"))
    assert rows[0]["deep_link"] == "http://links.test/?item=saved-12#/link/saved-12"
    assert "actual saved article text" in rows[0]["evidence"]
    assert r.manager.call_tool.call_args_list[1].args == ("mcp__links__read_link", {"id": "saved-12", "max_chars": 12000})


def test_hybrid_ranking_reports_the_missing_semantic_lane(monkeypatch):
    from src import embeddings
    monkeypatch.setattr(embeddings, "_load_persisted_endpoint", lambda: {})
    monkeypatch.delenv("EMBEDDING_URL", raising=False)
    result = cr.hybrid_rank([{"url": "https://source.test", "title": "Deadline", "evidence": "Project deadline Friday"}], "deadline")
    assert result["degraded"] is True
    assert "embedder" not in result["lanes"]
    assert result["hits"][0]["id"] == "https://source.test"


def test_source_selectors_do_not_poison_content_queries(monkeypatch):
    monkeypatch.setattr(cr.DeepResearcher, "_generate_queries", AsyncMock(return_value=[
        "FAUSTUS.md Deep Research", "Darwin documentos", "Links Faustus"]))
    assert asyncio.run(researcher()._generate_queries("question", "", 1)) == ["Deep Research", "documentos", "Faustus"]


def test_literal_sources_back_off_to_terms_without_losing_timeline(monkeypatch):
    r = researcher(source_ids=["notes"], source_base="https://faustus.example/")
    monkeypatch.setattr(cr, "_tools", lambda *a: [])
    r._local = AsyncMock(side_effect=lambda source, query: [{"url": "/notes?n=1", "content": "The recorded invoice"}] if query == "factura" else [])
    findings = asyncio.run(r._search_and_extract(["facturas archivadas"], "question"))
    assert [finding["url"] for finding in findings] == ["https://faustus.example/notes?n=1"]
    assert any(e["query"] == "facturas archivadas" and e["count"] == 0 for e in r.timeline)
    assert any(e["query"] == "factura" and e["count"] == 1 for e in r.timeline)


def test_linked_original_opens_at_a_late_cited_line(tmp_path, monkeypatch):
    from services import projects
    from urllib.parse import parse_qs, urlsplit
    monkeypatch.setenv("AUTH_ENABLED", "true")
    store = projects.ProjectStore(str(tmp_path / "store"))
    monkeypatch.setattr(projects, "get_store", lambda: store)
    original = tmp_path / "evidence.txt"
    original.write_text("Unrelated line.\n" * 30000 + "The recorded deadline is needle-Friday.\n", encoding="utf-8")
    project = store.create("Research fixture", workspace=str(tmp_path), owner="alice", scaffold_memory=False)
    link, _ = store.upsert_link(project["id"], {"kind": "file", "path": str(original), "label": "Original evidence",
        "enabled": True, "retrieval_policy": "auto"}, owner="alice")
    from src.context_engine.adapters.project_links import ProjectLinksSource
    # A source index can return a late passage even when a direct search's
    # bounded first window does not reach it. The reader must still open it.
    monkeypatch.setattr(ProjectLinksSource, "_matches", staticmethod(lambda *a: [SimpleNamespace(
        location={"line": 30001, "path": str(original)}, snippet="The recorded deadline is needle-Friday.")]))
    rows = cr.native_search("project_files", "alice", "needle-Friday", {})
    assert len(rows) == 1
    assert "needle-Friday" in rows[0]["evidence"]
    query = parse_qs(urlsplit(rows[0]["url"]).query)
    assert int(query["line"][0]) == 30001
    assert "needle-Friday" in cr.read_project_source("alice", project["id"], link["id"], line=30001)
    with pytest.raises(FileNotFoundError):
        cr.read_project_source("bob", project["id"], link["id"], line=30001)


def test_kafka_reader_extracts_document_pages():
    r = researcher()
    search = tool()
    search["server_id"] = "kafka"
    search["connector"]["preset_id"] = "kafka"
    reader = {"server_id": "kafka", "name": "doc_get", "qualified_name": "mcp__kafka__doc_get",
              "annotations": {"readOnlyHint": True}}
    r.manager = SimpleNamespace(get_all_tools=lambda: [reader], call_tool=AsyncMock(side_effect=[
        {"stdout": json.dumps({"items": [{"doc_id": "12", "title": "Invoice", "snippet": "Search excerpt"}]})},
        {"stdout": json.dumps({"pages": [{"text": "Page one."}, {"text": "Page two."}], "metadata": "not evidence"})},
    ]))
    rows = asyncio.run(r._connector(search, "invoice", "text"))
    assert rows[0]["evidence"] == "Page one.\n\nPage two."


def test_connected_brief_includes_filters_and_clarification(monkeypatch):
    monkeypatch.setattr(cr, "source_catalog", lambda owner: [{"id": "notes"}])
    llm = AsyncMock(return_value='{"plan": ["Read notes"], "clarification": ""}')
    monkeypatch.setattr(cr.DeepResearcher, "_llm", llm)
    brief = asyncio.run(cr.prepare_brief("question", endpoint="http://example.test", model="configured",
                                        source_ids=["notes"], source_filters={"notes": {"tag": "project"}},
                                        clarification="this month"))
    assert brief["plan"] == ["Read notes"]
    prompt = llm.call_args.args[0][0]["content"]
    assert '{"notes": {"tag": "project"}}' in prompt
    assert "this month" in prompt



def test_connected_checkpoint_preserves_original_app_links():
    finding = {"url": "http://kafka.test/?doc=12#/documentos/12", "title": "Invoice",
               "evidence": "Document text", "source_id": "mcp:kafka", "app_id": "kafka",
               "retrieval_lane": "text", "excerpt": False}
    saved = cr.ConnectedResearcher._trim_findings_for_checkpoint([None, finding])
    assert saved == [{"summary": "", **finding}]
    assert rh.ResearchHandler._extract_sources(saved)[0]["app_id"] == "kafka"
