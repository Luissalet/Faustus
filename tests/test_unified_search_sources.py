"""The projects and settings sources of src.unified_search and the built-in
`unified_search` MCP server (search_everything / search_sources)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from src import unified_search


def _store(projects):
    return SimpleNamespace(list=lambda owner=None: [p for p in projects if owner in (None, p.get("owner"))])


def test_projects_source_matches_every_token_and_links_the_project(monkeypatch):
    import services.projects as sp

    projects = [
        {"id": "p1", "owner": "ana", "name": "Mapas de Gulliver", "folder": "gulliver", "instructions": "", "workspace": "", "updated_at": 10},
        {"id": "p2", "owner": "ana", "name": "Otra cosa", "folder": "x", "instructions": "habla de mapas antiguos", "workspace": "", "updated_at": 20},
        {"id": "p3", "owner": "bob", "name": "Mapas de bob", "folder": "", "instructions": "", "workspace": "", "updated_at": 30},
    ]
    monkeypatch.setattr(sp, "get_store", lambda: _store(projects))

    rows = unified_search._projects("ana", "mapas", 8)
    assert [r["id"] for r in rows] == ["p2", "p1"]  # newest first, bob's never
    assert rows[1]["url"] == "/projects/p1" and rows[1]["type"] == "projects"
    assert unified_search._projects("ana", "mapas gulliver", 8)[0]["id"] == "p1"
    assert unified_search._projects("ana", "nada", 8) == []


def test_settings_source_finds_options_by_label_key_or_help():
    rows = unified_search._settings(None, "max steps", 5)
    assert rows, "the agent loop option 'Max steps per message' is found"
    hit = rows[0]
    assert hit["type"] == "settings" and hit["id"] == "agent_max_rounds"
    assert hit["url"] == "/settings?s=agent&q=agent_max_rounds"
    assert unified_search._settings(None, "agent_max_rounds", 5)[0]["id"] == "agent_max_rounds"
    assert len(unified_search._settings(None, "agent", 3)) == 3


def test_search_runs_the_new_sources_through_the_normal_path(monkeypatch):
    calls = []

    def fake(name):
        def run(owner, q, limit):
            calls.append(name)
            return [{"type": name, "id": "1", "title": q, "snippet": "", "url": "/x", "when": None, "score": 0.0}]
        return run

    for name in ("projects", "settings"):
        monkeypatch.setitem(unified_search._SOURCE_FUNCS, name, fake(name))
    out = unified_search.search("ana", "hola", types=["projects", "settings"])
    assert sorted(calls) == ["projects", "settings"]
    assert out["counts"] == {"projects": 1, "settings": 1}
    assert {"projects", "settings"} <= set(unified_search.SOURCES)


def test_mcp_server_lists_and_runs_search_everything(monkeypatch):
    from mcp_servers import unified_search_server as srv

    names = {t.name for t in asyncio.run(srv.list_tools())}
    assert names == {"search_everything", "search_sources"}
    for tool in asyncio.run(srv.list_tools()):
        assert tool.description and len(tool.description) > 60

    seen = {}

    def fake_search(owner, q, types=None, limit=8, **kw):
        seen.update(owner=owner, q=q, types=types, limit=limit, **kw)
        return {"query": q, "results": [], "counts": {}, "errors": {}, "elapsed_ms": 0}

    monkeypatch.setattr(unified_search, "search", fake_search)
    monkeypatch.setenv("ODYSSEUS_MCP_SEARCH_OWNER", "ana")
    out = asyncio.run(srv.call_tool("search_everything", {"q": "contrato", "types": ["chats"], "limit": 99, "before": "2026-11-01"}))
    assert json.loads(out[0].text)["query"] == "contrato"
    assert seen == {"owner": "ana", "q": "contrato", "types": ["chats"], "limit": 25, "after": "", "before": "2026-11-01"}

    sources = json.loads(asyncio.run(srv.call_tool("search_sources", {}))[0].text)["sources"]
    assert [s["type"] for s in sources] == list(unified_search.SOURCES)
    assert "Unknown tool" in asyncio.run(srv.call_tool("nope", {}))[0].text


def test_server_is_registered_as_builtin():
    from pathlib import Path

    from src import builtin_mcp

    script, label = builtin_mcp._BUILTIN_SERVERS["unified_search"]
    assert (Path(__file__).resolve().parent.parent / script).is_file()
    assert "Search everything" in label
