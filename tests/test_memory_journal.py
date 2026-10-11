"""Journal of memory changes with undo (#242): every store records create /
edit / delete with the turn that did it, and each change can be undone on
its own unless the item changed again since."""
from __future__ import annotations

import asyncio
import json

import pytest

from src import constants
from src import instincts
from src import memory as memmod
from src import memory_engine as engine
from src import memory_journal as mj
from src.brain import db as bdb
from src.brain import notes, vault

OWNER = "alice"


@pytest.fixture()
def data(tmp_path, monkeypatch):
    bdb.use_dir(str(tmp_path))
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(engine, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(instincts, "DATA_DIR", str(tmp_path))
    engine.set_vector_store(None)
    engine.clear_injected()
    monkeypatch.setattr(vault, "_projects_for_owner", lambda owner: [])
    yield tmp_path
    engine.reset_vector_store()
    engine.clear_injected()
    bdb.use_dir(None)


def _ops(store=None, **kw):
    return [(c["store"], c["op"]) for c in mj.list_changes(OWNER, store=store or "", **kw)]


# -- facts (memory.json) --------------------------------------------------------

def test_facts_create_edit_delete_are_journaled_and_bookkeeping_is_not(data):
    mgr = memmod.MemoryManager(str(data))
    entry = mgr.add_entry("Luis vive en Madrid", owner=OWNER)
    mgr.save([entry])
    entries = mgr.load_all_for_update()
    entries[0]["uses"] = 3                       # bookkeeping, not a change
    mgr.save(entries)
    entries[0]["text"] = "Luis vive en Valencia"
    mgr.save(entries)
    mgr.save([])
    assert _ops("facts") == [("facts", "delete"), ("facts", "edit"), ("facts", "create")]
    edit = mj.list_changes(OWNER, store="facts")[1]
    assert edit["label"] == "Luis vive en Valencia" and edit["before_label"] == "Luis vive en Madrid"


def test_facts_undo_in_reverse_order_restores_the_original(data):
    mgr = memmod.MemoryManager(str(data))
    entry = mgr.add_entry("prefiere té", owner=OWNER)
    mgr.save([entry])
    entries = mgr.load_all_for_update()
    entries[0]["text"] = "prefiere café"
    mgr.save(entries)
    mgr.save([])
    delete, edit, create = mj.list_changes(OWNER, store="facts")
    mj.undo(delete["id"], OWNER)
    assert [e["text"] for e in mgr.load_all_for_update()] == ["prefiere café"]
    mj.undo(edit["id"], OWNER)
    assert [e["text"] for e in mgr.load_all_for_update()] == ["prefiere té"]
    mj.undo(create["id"], OWNER)
    assert mgr.load_all_for_update() == []
    # undoing is not itself journaled, and each change is marked
    assert len(mj.list_changes(OWNER, store="facts")) == 3
    assert all(c["undone"] for c in mj.list_changes(OWNER))
    assert mj.list_changes(OWNER, include_undone=False) == []


def test_an_old_edit_cannot_be_undone_over_a_newer_one(data):
    mgr = memmod.MemoryManager(str(data))
    mgr.save([mgr.add_entry("uno", owner=OWNER)])
    entries = mgr.load_all_for_update()
    entries[0]["text"] = "dos"
    mgr.save(entries)
    entries[0]["text"] = "tres"
    mgr.save(entries)
    newer, older, _ = mj.list_changes(OWNER, store="facts")
    with pytest.raises(mj.UndoConflict):
        mj.undo(older["id"], OWNER)
    assert mgr.load_all_for_update()[0]["text"] == "tres"
    mj.undo(newer["id"], OWNER)
    mj.undo(older["id"], OWNER)
    assert mgr.load_all_for_update()[0]["text"] == "uno"


def test_someone_elses_change_or_a_second_undo_is_refused(data):
    mgr = memmod.MemoryManager(str(data))
    mgr.save([mgr.add_entry("x", owner=OWNER)])
    (change,) = mj.list_changes(OWNER)
    with pytest.raises(mj.UndoError):
        mj.undo(change["id"], "mallory")
    mj.undo(change["id"], OWNER)
    with pytest.raises(mj.UndoError):
        mj.undo(change["id"], OWNER)


# -- attribution ----------------------------------------------------------------

def test_changes_carry_the_running_turn_or_an_explicit_attribution(data):
    from src import tool_execution as te
    mgr = memmod.MemoryManager(str(data))
    token = te._active_turn_options.set({"turn_id": "sess-9:1700000000000", "run_id": "run-1"})
    try:
        mgr.save([mgr.add_entry("en el turno", owner=OWNER)])
    finally:
        te._active_turn_options.reset(token)
    with mj.attribution(session_id="sess-bg", source="memory_extractor"):
        mgr.save(mgr.load_all_for_update() + [mgr.add_entry("después", owner=OWNER)])
    background, in_turn = mj.list_changes(OWNER)
    assert (in_turn["session_id"], in_turn["run_id"], in_turn["turn_id"]) == ("sess-9", "run-1", "sess-9:1700000000000")
    assert (background["session_id"], background["source"]) == ("sess-bg", "memory_extractor")
    assert [c["label"] for c in mj.for_turn("sess-9:1700000000000")] == ["en el turno"]
    assert [c["label"] for c in mj.list_changes(OWNER, session_id="sess-bg")] == ["después"]


# -- instincts --------------------------------------------------------------------

def test_instincts_are_journaled_and_a_deleted_one_comes_back(data):
    rec = instincts.add(OWNER, trigger="cuando falle un test", action="lee el traceback entero", scope="global")
    instincts.confirm(OWNER, rec["id"])                       # confidence only
    instincts.edit(OWNER, rec["id"], action="lee el traceback y el test")
    assert instincts.delete(OWNER, rec["id"])
    assert _ops("instincts") == [("instincts", "delete"), ("instincts", "edit"), ("instincts", "create")]
    delete = mj.list_changes(OWNER, store="instincts")[0]
    mj.undo(delete["id"], OWNER)
    assert instincts._load_raw(OWNER)[rec["id"]]["action"] == "lee el traceback y el test"


# -- learned memory (engine) --------------------------------------------------------

def test_a_deleted_learned_memory_comes_back_and_a_forgotten_one_does_not(data):
    item = engine.add_item("usa pnpm en este repo", owner=OWNER, project="p", level="semantic")
    engine.delete_item(item["id"])
    delete, create = mj.list_changes(OWNER, store="rules")
    assert (delete["op"], create["op"]) == ("delete", "create")
    mj.undo(delete["id"], OWNER)
    assert engine.get_item(item["id"])["text"] == "usa pnpm en este repo"
    mj.undo(create["id"], OWNER)
    assert engine.get_item(item["id"]) is None
    # a definitive forget (tombstone) is not undoable: see test_memory_journal_privacy.py
    other = engine.add_item("usa yarn en este repo", owner=OWNER, project="p", level="semantic")
    engine.forget(other["id"], reason="wrong")
    assert engine.is_tombstoned("usa yarn en este repo", OWNER, "p", "semantic")
    assert all(c["purged"] for c in mj.list_changes(OWNER, store="rules") if c["item_id"] == other["id"])


# -- second-brain notes -----------------------------------------------------------------

def test_notes_edit_and_delete_are_journaled_and_undone_through_the_trash(data):
    notes.write_note(OWNER, "Notes/plan.md", "# Plan\nprimera versión\n")
    notes.write_note(OWNER, "Notes/plan.md", "# Plan\nsegunda versión\n")
    notes.delete_note(OWNER, "Notes/plan.md")
    delete, edit, create = mj.list_changes(OWNER, store="notes")
    assert (delete["op"], edit["op"], create["op"]) == ("delete", "edit", "create")
    mj.undo(delete["id"], OWNER)
    assert "segunda versión" in notes.read_note(OWNER, "Notes/plan.md")["content"]
    mj.undo(edit["id"], OWNER)
    assert "primera versión" in notes.read_note(OWNER, "Notes/plan.md")["content"]


# -- API and MCP -------------------------------------------------------------------------

def test_routes_list_and_undo_with_409_on_conflict(data, monkeypatch):
    pytest.importorskip("starlette.testclient")
    from fastapi import FastAPI
    from starlette.testclient import TestClient
    import routes.memory_journal_routes as r
    monkeypatch.setattr(r, "get_current_user", lambda request: OWNER)
    app = FastAPI()
    app.include_router(r.setup_memory_journal_routes())
    client = TestClient(app)
    mgr = memmod.MemoryManager(str(data))
    mgr.save([mgr.add_entry("a", owner=OWNER)])
    entries = mgr.load_all_for_update()
    entries[0]["text"] = "b"
    mgr.save(entries)
    entries[0]["text"] = "c"
    mgr.save(entries)
    body = client.get("/api/memory-journal/changes?store=facts").json()
    assert body["count"] == 3
    older = body["changes"][1]["id"]
    assert client.post(f"/api/memory-journal/changes/{older}/undo").status_code == 409
    assert client.post("/api/memory-journal/changes/nope/undo").status_code == 404
    assert client.get("/api/memory-journal/changes?store=bogus").status_code == 400
    assert client.post(f"/api/memory-journal/changes/{body['changes'][0]['id']}/undo").json()["ok"] is True


def test_mcp_memory_changes_lists_and_undoes(data, monkeypatch):
    pytest.importorskip("mcp")
    import mcp_servers.memory_server as srv
    monkeypatch.setenv("ODYSSEUS_MCP_MEMORY_OWNER", OWNER)
    mgr = memmod.MemoryManager(str(data))
    mgr.save([mgr.add_entry("desde mcp", owner=OWNER)])
    tools = [t.name for t in asyncio.run(srv.list_tools())]
    assert "memory_changes" in tools
    listed = json.loads(asyncio.run(srv.call_tool("memory_changes", {"action": "list"}))[0].text)
    assert listed["count"] == 1
    out = json.loads(asyncio.run(srv.call_tool("memory_changes", {"action": "undo",
                                                                   "change_id": listed["changes"][0]["id"]}))[0].text)
    assert out["ok"] is True and mgr.load_all_for_update() == []
