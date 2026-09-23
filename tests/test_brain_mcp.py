"""The brain MCP server (mcp_servers/brain_server.py).

Same three things pinned for `context_engine_server.py` are pinned here:

* importing the module must not touch the brain store — `_ensure_init()` is
  paid only on the first real call;
* `list_tools()` must return all eight tools with a schema a client can
  validate against;
* a write with no `ODYSSEUS_MCP_BRAIN_OWNER` must be refused and must name
  the variable, before the brain is ever loaded.

The functional tests exercise the handlers in-process, like
`test_context_engine_mcp.py` does — no subprocess, no real stdio transport —
against an isolated brain store and memory store per test.
"""

import asyncio

import pytest

pytest.importorskip("mcp")

import mcp_servers.brain_server as bs
from src import memory_engine as engine
from src.brain import db as brain_db

TOOL_NAMES = {
    "brain_search", "brain_read_note", "brain_write_note", "brain_append_note",
    "brain_entity", "brain_timeline", "brain_graph_neighbors", "brain_sync",
}

OWNER = "ada"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for key in bs._OWNER_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)

    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    monkeypatch.setattr(engine, "DATA_DIR", str(memory_dir))
    engine.set_vector_store(None)
    engine.clear_injected()

    brain_db.use_dir(str(tmp_path / "brain"))
    # A fresh store per test means a fresh call to `_ensure_init()` would
    # otherwise reuse cached module references that are still valid (the
    # modules themselves are not per-directory), so no reset is needed there.

    yield

    brain_db.use_dir(None)
    engine.clear_injected()


def _call(name, arguments):
    return asyncio.run(bs.call_tool(name, arguments))


def test_importing_the_server_starts_nothing():
    """Must run before any other test in this file calls `_ensure_init()`."""
    assert bs._initialized is False
    assert bs._engine == {}
    assert bs.server.name == "brain"


def test_list_tools_returns_every_usable_schema():
    tools = asyncio.run(bs.list_tools())
    assert {t.name for t in tools} == TOOL_NAMES
    for tool in tools:
        assert tool.description and len(tool.description) > 60, tool.name
        schema = tool.inputSchema
        assert schema["type"] == "object", tool.name
        properties = schema.get("properties") or {}
        for field, spec in properties.items():
            assert isinstance(spec, dict) and "type" in spec, f"{tool.name}.{field}"
        for required in schema.get("required", []):
            assert required in properties, f"{tool.name}.{required}"


def test_writes_without_an_owner_are_refused_and_say_which_variable():
    for name, arguments in (
        ("brain_write_note", {"title": "x", "content": "hello"}),
        ("brain_append_note", {"path": "Notes/x.md", "content": "more"}),
        ("brain_sync", {}),
    ):
        out = _call(name, arguments)
        assert "ODYSSEUS_MCP_BRAIN_OWNER" in out[0].text, name


def test_reads_work_without_an_owner():
    """Reads degrade to install-wide (owner ""), same policy as the context
    and memory servers — a single-user install already means this."""
    out = _call("brain_search", {"query": "anything"})
    assert '"ok": true' in out[0].text.lower()


def test_an_unknown_tool_is_a_message_not_an_exception():
    out = _call("brain_nope", {})
    assert "Unknown tool" in out[0].text


# ── functional, with an owner configured ─────────────────────────────────

@pytest.fixture()
def owned(monkeypatch):
    monkeypatch.setenv("ODYSSEUS_MCP_BRAIN_OWNER", OWNER)
    return OWNER


def test_write_then_read_a_new_note(owned):
    out = _call("brain_write_note",
               {"title": "Coffee ideas", "content": "Try a Kalita pour-over."})
    assert '"ok": true' in out[0].text.lower()
    assert "created" in out[0].text

    found = _call("brain_search", {"query": "Kalita"})
    assert "Coffee ideas" in found[0].text

    hit_path = "Notes/Coffee ideas.md"
    read = _call("brain_read_note", {"path": hit_path})
    assert "Kalita" in read[0].text


def test_append_adds_without_removing_existing_text(owned):
    _call("brain_write_note", {"title": "Log", "content": "First entry."})
    _call("brain_append_note", {"path": "Notes/Log.md", "content": "Second entry."})
    read = _call("brain_read_note", {"path": "Notes/Log.md"})
    assert "First entry." in read[0].text
    assert "Second entry." in read[0].text


def test_append_to_unknown_note_is_a_message_not_an_exception(owned):
    out = _call("brain_append_note", {"path": "Notes/nope.md", "content": "x"})
    assert "no such note" in out[0].text.lower()


def test_entity_by_name_and_timeline(owned):
    from src.brain import entities

    entities.upsert_entity(OWNER, "Bruno Villanueva", type="person")
    profile = _call("brain_entity", {"name": "Bruno Villanueva"})
    assert '"ok": true' in profile[0].text.lower()
    assert "Bruno Villanueva" in profile[0].text

    timeline = _call("brain_timeline", {"name": "Bruno Villanueva"})
    assert '"ok": true' in timeline[0].text.lower()


def test_entity_unknown_name_is_a_message(owned):
    out = _call("brain_entity", {"name": "Nobody Here"})
    assert "no entity matches" in out[0].text.lower()


def test_cross_entity_timeline_without_a_name(owned):
    engine.add_item("met Cordera Labs about the roadmap", owner=OWNER)
    out = _call("brain_timeline", {"query": "roadmap"})
    assert '"ok": true' in out[0].text.lower()


def test_graph_neighbors_notes_and_entities(owned):
    _call("brain_write_note", {"title": "Hub", "content": "Links to [[Coffee ideas]]."})
    _call("brain_write_note", {"title": "Coffee ideas", "content": "Beans."})

    notes_graph = _call("brain_graph_neighbors", {"path": "Notes/Hub.md"})
    assert '"scope": "notes"' in notes_graph[0].text

    from src.brain import entities
    entities.upsert_entity(OWNER, "Cordera Labs", type="organization")
    entity_graph = _call("brain_graph_neighbors", {})
    assert '"scope": "entities"' in entity_graph[0].text


def test_sync_with_an_owner_runs(owned):
    out = _call("brain_sync", {})
    assert '"ok": true' in out[0].text.lower()
    assert "report" in out[0].text
