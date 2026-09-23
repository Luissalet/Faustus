"""tests/test_prior_art_mcp.py — the prior_art MCP server
(mcp_servers/prior_art_server.py).

Same discipline `test_brain_mcp.py`/`test_context_engine_mcp.py` use: the
handlers are exercised in-process (no subprocess, no real stdio transport),
and `src.prior_art` itself is monkeypatched so this file is only about the
server's own tool surface and dispatch, not the network behaviour already
covered by `tests/test_prior_art.py`.
"""

import asyncio

import pytest

pytest.importorskip("mcp")

import mcp_servers.prior_art_server as srv

TOOL_NAMES = {"prior_art_rubric", "prior_art_verify", "prior_art_search", "prior_art_report"}


@pytest.fixture(autouse=True)
def isolated():
    srv._initialized = False
    srv._engine.clear()
    yield
    srv._initialized = False
    srv._engine.clear()


def _call(name, arguments):
    return asyncio.run(srv.call_tool(name, arguments))


def test_importing_the_server_starts_nothing():
    assert srv._initialized is False
    assert srv._engine == {}
    assert srv.server.name == "prior_art"


def test_list_tools_returns_every_usable_schema():
    tools = asyncio.run(srv.list_tools())
    assert {t.name for t in tools} == TOOL_NAMES
    for tool in tools:
        assert tool.description and len(tool.description) > 60, tool.name
        schema = tool.inputSchema
        assert schema["type"] == "object", tool.name
        for required in schema.get("required", []):
            assert required in (schema.get("properties") or {}), f"{tool.name}.{required}"


def test_an_unknown_tool_is_a_message_not_an_exception():
    out = _call("prior_art_nope", {})
    assert "Unknown tool" in out[0].text


def test_rubric_call(monkeypatch):
    monkeypatch.setattr("src.prior_art.rubric",
                        lambda idea, **kw: {"idea": idea, "instructions": "do it"})
    out = _call("prior_art_rubric", {"idea": "a markdown to pdf converter"})
    assert '"ok": true' in out[0].text.lower()
    assert "do it" in out[0].text


def test_rubric_without_idea_is_a_message():
    out = _call("prior_art_rubric", {})
    assert "idea" in out[0].text.lower()


def test_verify_call(monkeypatch):
    monkeypatch.setattr("src.prior_art.verify",
                        lambda slate, **kw: {"verified": True, "table": "| a |", "id": "PA-000001"})
    out = _call("prior_art_verify", {"slate": {"components": []}})
    assert '"ok": true' in out[0].text.lower()
    assert "PA-000001" in out[0].text


def test_verify_without_slate_is_a_message():
    out = _call("prior_art_verify", {})
    assert "slate" in out[0].text.lower()


def test_search_call(monkeypatch):
    monkeypatch.setattr("src.prior_art.search",
                        lambda query, **kw: {"verified": True, "results": [{"full_name": "psf/requests"}]})
    out = _call("prior_art_search", {"query": "http client"})
    assert '"ok": true' in out[0].text.lower()
    assert "psf/requests" in out[0].text


def test_search_without_query_is_a_message():
    out = _call("prior_art_search", {})
    assert "query" in out[0].text.lower()


def test_report_by_id_and_missing(monkeypatch):
    def fake_report(report_id):
        return {"id": report_id, "idea": "x"} if report_id == "PA-000001" else None

    monkeypatch.setattr("src.prior_art.report", fake_report)
    found = _call("prior_art_report", {"id": "PA-000001"})
    assert '"ok": true' in found[0].text.lower()

    missing = _call("prior_art_report", {"id": "PA-999999"})
    assert "no such report" in missing[0].text.lower()


def test_report_listing(monkeypatch):
    monkeypatch.setattr("src.prior_art.reports", lambda limit: [{"id": "PA-000001"}])
    out = _call("prior_art_report", {})
    assert '"ok": true' in out[0].text.lower()
    assert "PA-000001" in out[0].text


def test_a_broken_engine_import_is_a_message_not_an_exception(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def broken_import(name, *a, **kw):
        if name == "src":
            raise ImportError("boom")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", broken_import)
    out = _call("prior_art_rubric", {"idea": "x"})
    assert "could not be loaded" in out[0].text
