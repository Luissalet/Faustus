"""tests/test_brain_tool.py — the `brain` agent tool executor.

`BrainTool.execute(content, ctx)` is exercised directly, the same shape
`src/tool_execution.py` calls every `TOOL_HANDLERS` entry with. Owner comes
from `ctx["owner"]`, never from `content` — mirrored from
`context_recall_tools.py`'s own discipline.

Isolation matches `tests/test_brain_maintenance.py` / `test_brain_mcp.py`:
a fresh brain store and a fresh memory store per test.
"""

from __future__ import annotations

import asyncio

import pytest

from src import memory_engine as engine
from src.agent_tools.brain_tools import BrainTool
from src.brain import db as brain_db

OWNER = "ada"
tool = BrainTool()


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    monkeypatch.setattr(engine, "DATA_DIR", str(memory_dir))
    engine.set_vector_store(None)
    engine.clear_injected()

    brain_db.use_dir(str(tmp_path / "brain"))

    yield

    brain_db.use_dir(None)
    engine.clear_injected()


def _run(content, owner=OWNER):
    return asyncio.run(tool.execute(content, {"owner": owner}))


def test_unknown_action_is_an_error():
    out = _run({"action": "fly"})
    assert out["exit_code"] == 1
    assert "unknown action" in out["error"]


def test_invalid_json_string_is_an_error():
    out = _run("{not json")
    assert out["exit_code"] == 1
    assert "invalid arguments" in out["error"]


def test_write_creates_a_note_then_read_finds_it():
    out = _run({"action": "write", "title": "Coffee ideas",
               "content": "Try a Kalita pour-over."})
    assert out["exit_code"] == 0
    assert out["action"] == "created"
    path = out["note"]["path"]
    assert path == "Notes/Coffee ideas.md"

    read = _run({"action": "read", "path": path})
    assert read["exit_code"] == 0
    assert "Kalita" in read["output"]


def test_search_finds_the_created_note():
    _run({"action": "write", "title": "Roast notes", "content": "Villanueva blend, light roast."})
    out = _run({"action": "search", "query": "Villanueva"})
    assert out["exit_code"] == 0
    assert any("Roast notes" in n["title"] for n in out["notes"])


def test_search_without_query_is_an_error():
    out = _run({"action": "search"})
    assert out["exit_code"] == 1


def test_append_adds_without_erasing():
    created = _run({"action": "write", "title": "Log", "content": "First entry."})
    path = created["note"]["path"]
    out = _run({"action": "append", "path": path, "content": "Second entry."})
    assert out["exit_code"] == 0
    read = _run({"action": "read", "path": path})
    assert "First entry." in read["output"]
    assert "Second entry." in read["output"]


def test_write_edit_replaces_a_free_note_body():
    created = _run({"action": "write", "title": "Draft", "content": "v1"})
    path = created["note"]["path"]
    edited = _run({"action": "write", "path": path, "content": "v2"})
    assert edited["exit_code"] == 0
    read = _run({"action": "read", "path": path})
    assert "v2" in read["output"]
    assert "v1" not in read["output"]


def test_read_unknown_note_is_an_error():
    out = _run({"action": "read", "path": "Notes/nope.md"})
    assert out["exit_code"] == 1
    assert "no such note" in out["error"].lower()


def test_write_with_no_path_and_no_title_is_an_error():
    out = _run({"action": "write", "content": "orphan"})
    assert out["exit_code"] == 1


def test_entity_by_name_and_unknown_name():
    from src.brain import entities

    entities.upsert_entity(OWNER, "Bruno Villanueva", type="person")
    out = _run({"action": "entity", "name": "Bruno Villanueva"})
    assert out["exit_code"] == 0
    assert out["profile"]["entity"]["name"] == "Bruno Villanueva"

    missing = _run({"action": "entity", "name": "Nobody Here"})
    assert missing["exit_code"] == 1
    assert "no entity matches" in missing["error"]


def test_entity_with_neither_id_nor_name_is_an_error():
    out = _run({"action": "entity"})
    assert out["exit_code"] == 1


def test_timeline_cross_entity_and_per_entity():
    from src.brain import entities

    engine.add_item("met Cordera Labs about the roadmap", owner=OWNER)
    cross = _run({"action": "timeline", "query": "roadmap"})
    assert cross["exit_code"] == 0
    assert "timeline" in cross

    entities.upsert_entity(OWNER, "Cordera Labs", type="organization")
    per_entity = _run({"action": "timeline", "name": "Cordera Labs"})
    assert per_entity["exit_code"] == 0
    assert "entity_id" in per_entity


def test_neighbors_notes_and_entities_scope():
    _run({"action": "write", "title": "Hub", "content": "Links to [[Coffee ideas]]."})
    _run({"action": "write", "title": "Coffee ideas", "content": "Beans."})
    notes_graph = _run({"action": "neighbors", "path": "Notes/Hub.md"})
    assert notes_graph["exit_code"] == 0
    assert notes_graph["scope"] == "notes"

    from src.brain import entities
    entities.upsert_entity(OWNER, "Cordera Labs", type="organization")
    entity_graph = _run({"action": "neighbors"})
    assert entity_graph["exit_code"] == 0
    assert entity_graph["scope"] == "entities"


def test_daily_creates_todays_note():
    out = _run({"action": "daily"})
    assert out["exit_code"] == 0
    assert out["note"]["path"].startswith("Daily/")


def test_owner_isolation_search_does_not_cross_owners():
    _run({"action": "write", "title": "Private", "content": "Ada's secret plan."},
        owner="ada")
    out = _run({"action": "search", "query": "secret"}, owner="mallory")
    assert out["exit_code"] == 0
    assert out["notes"] == []
