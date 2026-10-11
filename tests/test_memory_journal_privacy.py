"""Journal of memory changes (#242), review fixes: the journal follows what
the person erased (wipe, forget, clear history), retention is deterministic,
stored text is capped, undo explains itself and never duplicates, and the MCP
tool is catalogued and owner-scoped."""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3

import pytest

from src import constants
from src import instincts
from src import memory as memmod
from src import memory_engine as engine
from src import memory_journal as mj
from src.brain import db as bdb
from src.brain import notes, vault

OWNER = "alice"
DAY = 86400


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


def _db_bytes(data) -> bytes:
    blob = b""
    for suffix in ("", "-wal", "-journal"):
        path = os.path.join(str(data), mj.DB_FILENAME + suffix)
        if os.path.exists(path):
            with open(path, "rb") as fh:
                blob += fh.read()
    return blob


def _client(data, monkeypatch, owner=OWNER):
    pytest.importorskip("starlette.testclient")
    from fastapi import FastAPI
    from starlette.testclient import TestClient
    import routes.memory_journal_routes as r
    monkeypatch.setattr(r, "get_current_user", lambda request: owner)
    app = FastAPI()
    app.include_router(r.setup_memory_journal_routes())
    return TestClient(app)


# -- P2: wipe, forget and clear -------------------------------------------------

def test_wiping_memory_drops_the_journal_and_undo_cannot_resurrect(data, monkeypatch):
    """The repro: add a fact, delete it, wipe the memory -> the delete used to
    stay undoable and bring the text back."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from core.database import Base
    import routes.admin_wipe.admin_wipe_routes as wipe
    factory = sessionmaker(bind=create_engine("sqlite:///:memory:"))
    Base.metadata.create_all(bind=factory.kw["bind"])
    monkeypatch.setattr(wipe, "SessionLocal", factory)
    monkeypatch.setattr(wipe, "require_admin", lambda request: None)
    monkeypatch.setattr(wipe, "DATA_DIR", str(data))

    mgr = memmod.MemoryManager(str(data))
    entry = mgr.add_entry("mi DNI de prueba es 0000", owner=OWNER)
    mgr.save([entry])
    mgr.save([])
    delete = mj.list_changes(OWNER, store="facts")[0]
    assert delete["op"] == "delete" and "0000" in _db_bytes(data).decode("utf-8", "ignore")

    from fastapi import Request
    router = wipe.setup_admin_wipe_routes(session_manager=None)
    handler = next(r for r in router.routes if r.path == "/api/admin/wipe/{kind}").endpoint
    assert handler(kind="memory", request=Request(scope={"type": "http"}))["status"] == "deleted"

    assert mj.list_changes(OWNER) == []
    assert mj.get_change(delete["id"]) is None
    with pytest.raises(mj.UndoError) as caught:
        mj.undo(delete["id"], OWNER)
    assert caught.value.code == "unknown"
    assert mgr.load_all_for_update() == []
    assert "0000" not in _db_bytes(data).decode("utf-8", "ignore")      # not even in free pages


def test_a_row_that_survives_a_wipe_still_cannot_be_undone(data):
    mgr = memmod.MemoryManager(str(data))
    mgr.save([mgr.add_entry("algo", owner=OWNER)])
    mgr.save([])
    delete = mj.list_changes(OWNER, store="facts")[0]
    with mj._connect() as conn:                                      # a wipe that left the row behind
        conn.execute("INSERT INTO wipes (store, ts) VALUES ('facts', ?)", (delete["ts"] + 1,))
    with pytest.raises(mj.UndoGone) as caught:
        mj.undo(delete["id"], OWNER)
    assert caught.value.code == "store_wiped"
    assert mgr.load_all_for_update() == []


def test_wiping_one_store_leaves_the_others(data):
    mgr = memmod.MemoryManager(str(data))
    mgr.save([mgr.add_entry("hecho", owner=OWNER)])
    instincts.add(OWNER, trigger="cuando falle un test", action="lee el traceback", scope="global")
    assert mj.wipe_store("facts") == 1
    assert [(c["store"]) for c in mj.list_changes(OWNER)] == ["instincts"]


def test_a_definitive_forget_erases_the_text_and_cannot_be_undone(data):
    item = engine.add_item("mi contraseña de prueba es hunter2", owner=OWNER, project="p", level="semantic")
    engine.forget(item["id"], reason="privacy")
    rows = mj.list_changes(OWNER, store="rules")
    assert [(c["op"], c["purged"], c["label"]) for c in rows] == [("delete", True, ""), ("create", True, "")]
    assert "hunter2" not in _db_bytes(data).decode("utf-8", "ignore")
    with pytest.raises(mj.UndoGone) as caught:
        mj.undo(rows[0]["id"], OWNER)
    assert caught.value.code == "purged"
    assert engine.get_item(item["id"]) is None and engine.is_tombstoned(
        "mi contraseña de prueba es hunter2", OWNER, "p", "semantic")


def test_a_correction_keeps_its_journal_because_it_is_undoable(data):
    item = engine.add_item("usa npm", owner=OWNER, project="p", level="semantic")
    new = engine.correct(item["id"], "usa pnpm")
    rows = mj.list_changes(OWNER, store="rules")
    assert rows and not any(c["purged"] for c in rows)
    assert any(c["label"] == "usa npm" for c in rows) and any(c["label"] == "usa pnpm" for c in rows)
    assert new and engine.get_item(new["id"])["text"] == "usa pnpm"


def test_clear_history_is_owner_scoped_and_leaves_the_memories(data, monkeypatch):
    mgr = memmod.MemoryManager(str(data))
    mgr.save([mgr.add_entry("de alice con secreto-alfa", owner=OWNER)])
    mgr.save(mgr.load_all_for_update() + [mgr.add_entry("de bob", owner="bob")])
    client = _client(data, monkeypatch)
    assert client.delete("/api/memory-journal/changes").json() == {"status": "success", "deleted": 1}
    assert mj.list_changes(OWNER) == [] and [c["label"] for c in mj.list_changes("bob")] == ["de bob"]
    assert len(mgr.load_all_for_update()) == 2                         # memories untouched
    assert "secreto-alfa" not in _db_bytes(data).decode("utf-8", "ignore")


def test_undo_route_answers_with_a_code_and_the_right_status(data, monkeypatch):
    client = _client(data, monkeypatch)
    mgr = memmod.MemoryManager(str(data))
    mgr.save([mgr.add_entry("a", owner=OWNER)])
    entries = mgr.load_all_for_update()
    entries[0]["text"] = "b"
    mgr.save(entries)
    entries[0]["text"] = "c"
    mgr.save(entries)
    newer, older, _ = client.get("/api/memory-journal/changes?store=facts").json()["changes"]
    refused = client.post(f"/api/memory-journal/changes/{older['id']}/undo")
    assert refused.status_code == 409
    detail = refused.json()["detail"]
    assert detail["code"] == "later_change" and detail["blocking"]["id"] == newer["id"]
    assert "c" in detail["message"] and detail["blocking"]["op"] == "edit"
    # a forgotten learned memory: 410 with its own code
    item = engine.add_item("dato olvidado", owner=OWNER, project="p", level="semantic")
    engine.forget(item["id"])
    gone = client.get("/api/memory-journal/changes?store=rules").json()["changes"][0]
    erased = client.post(f"/api/memory-journal/changes/{gone['id']}/undo")
    assert erased.status_code == 410 and erased.json()["detail"]["code"] == "purged"
    assert client.post("/api/memory-journal/changes/nope/undo").json()["detail"]["code"] == "unknown"


# -- P3: retention, size ---------------------------------------------------------------

def test_retention_runs_at_startup_and_at_most_once_a_day(data, monkeypatch):
    t0 = 1_800_000_000.0
    monkeypatch.setattr(mj, "_now", lambda: t0)
    mgr = memmod.MemoryManager(str(data))
    mgr.save([mgr.add_entry("viejo", owner=OWNER)])                       # first record: the pass runs, nothing to prune
    with mj._connect() as conn:
        old = t0 - (mj.KEEP_DAYS + 5) * DAY
        conn.execute("UPDATE changes SET ts = ?", (old,))
        conn.execute("INSERT INTO changes (id, ts, owner, store, op, item_id, label) VALUES ('x', ?, ?, 'facts', 'create', 'i', 'otro viejo')",
                     (old, OWNER))
    monkeypatch.setattr(mj, "_now", lambda: t0 + 3600)                    # same day: not due, old rows stay
    mgr.save(mgr.load_all_for_update() + [mgr.add_entry("nuevo uno", owner=OWNER)])
    assert len(mj.list_changes(OWNER)) == 3
    monkeypatch.setattr(mj, "_now", lambda: t0 + DAY + 60)                # a day later: due
    mgr.save(mgr.load_all_for_update() + [mgr.add_entry("nuevo dos", owner=OWNER)])
    assert sorted(c["label"] for c in mj.list_changes(OWNER)) == ["nuevo dos", "nuevo uno"]


def test_startup_prune_is_explicit_idempotent_and_does_not_create_a_journal(data, monkeypatch):
    assert mj.prune() == 0 and not os.path.exists(os.path.join(str(data), mj.DB_FILENAME))
    t0 = 1_800_000_000.0
    monkeypatch.setattr(mj, "_now", lambda: t0)
    mgr = memmod.MemoryManager(str(data))
    mgr.save([mgr.add_entry("viejo", owner=OWNER)])
    with mj._connect() as conn:
        conn.execute("UPDATE changes SET ts = ?", (t0 - (mj.KEEP_DAYS + 1) * DAY,))
    assert mj.prune() == 0                                                # already ran today
    assert mj.prune(force=True) == 1
    assert mj.list_changes(OWNER) == []


def test_a_big_prune_compacts_the_file(data, monkeypatch):
    t0 = 1_800_000_000.0
    monkeypatch.setattr(mj, "_now", lambda: t0)
    mgr = memmod.MemoryManager(str(data))
    mgr.save([mgr.add_entry("x" * 15000 + " semilla", owner=OWNER)])
    with mj._connect() as conn:
        row = dict(conn.execute("SELECT * FROM changes").fetchone())
        for n in range(mj.VACUUM_AFTER_ROWS + 5):
            row["id"] = f"old{n}"
            row["ts"] = t0 - (mj.KEEP_DAYS + 2) * DAY
            conn.execute("INSERT INTO changes VALUES (" + ",".join("?" * len(row)) + ")", list(row.values()))
    big = os.path.getsize(os.path.join(str(data), mj.DB_FILENAME))
    assert mj.prune(force=True) >= mj.VACUUM_AFTER_ROWS
    assert os.path.getsize(os.path.join(str(data), mj.DB_FILENAME)) < big / 4


def test_stored_text_is_capped_and_an_edit_of_a_huge_note_cannot_be_undone(data):
    first = "# Grande\n" + "a" * 30000
    notes.write_note(OWNER, "Notes/grande.md", first)
    notes.write_note(OWNER, "Notes/grande.md", first + "\nfin")
    edit, create = mj.list_changes(OWNER, store="notes")
    assert edit["truncated"] and create["truncated"]
    full = mj.get_change(edit["id"])
    assert len(full["before"]["content"]) == mj.MAX_FIELD_CHARS and len(full["after"]["content"]) == mj.MAX_FIELD_CHARS
    assert full["after"][mj.TRUNCATED_KEY]["content"]["chars"] == len(first) + 4
    assert len(full["after"][mj.TRUNCATED_KEY]["content"]["sha256"]) == 64
    with pytest.raises(mj.UndoError) as caught:
        mj.undo(edit["id"], OWNER)
    assert caught.value.code == "truncated"
    assert notes.read_note(OWNER, "Notes/grande.md")["content"].endswith("fin")
    raw = sqlite3.connect(mj.db_path()).execute("SELECT length(before)+length(after) FROM changes WHERE id = ?",
                                                (edit["id"],)).fetchone()[0]
    assert raw < 2 * (mj.MAX_FIELD_CHARS + 2000)


def test_a_deleted_note_is_not_stored_twice(data):
    notes.write_note(OWNER, "Notes/dos.md", "# Dos\nsolo en la papelera\n")
    notes.delete_note(OWNER, "Notes/dos.md")
    delete = mj.list_changes(OWNER, store="notes")[0]
    assert mj.get_change(delete["id"])["before"].get("content") is None
    mj.undo(delete["id"], OWNER)
    assert "solo en la papelera" in notes.read_note(OWNER, "Notes/dos.md")["content"]


# -- P3: undo that duplicates, later changes ----------------------------------------------

def test_undoing_a_delete_when_the_text_came_back_does_not_duplicate(data):
    mgr = memmod.MemoryManager(str(data))
    mgr.save([mgr.add_entry("Luis vive en Madrid", owner=OWNER)])
    mgr.save([])
    mgr.save([mgr.add_entry("luis  vive en madrid", owner=OWNER)])        # same text, another id
    delete = [c for c in mj.list_changes(OWNER, store="facts") if c["op"] == "delete"][0]
    with pytest.raises(mj.UndoConflict) as caught:
        mj.undo(delete["id"], OWNER)
    assert caught.value.code == "duplicate"
    assert len(mgr.load_all_for_update()) == 1


def test_duplicate_guard_for_learned_memory_instincts_and_notes(data):
    item = engine.add_item("usa pnpm aquí", owner=OWNER, project="p", level="semantic")
    engine.delete_item(item["id"])
    engine.add_item("usa pnpm aquí", owner=OWNER, project="p", level="semantic")
    delete = [c for c in mj.list_changes(OWNER, store="rules") if c["op"] == "delete"][0]
    with pytest.raises(mj.UndoConflict) as caught:
        mj.undo(delete["id"], OWNER)
    assert caught.value.code == "duplicate"

    rec = instincts.add(OWNER, trigger="al fallar un test", action="lee todo el traceback", scope="global")
    instincts.delete(OWNER, rec["id"])
    instincts.add(OWNER, trigger="Al fallar un test", action="lee todo el traceback", scope="global")
    delete = [c for c in mj.list_changes(OWNER, store="instincts") if c["op"] == "delete"][0]
    with pytest.raises(mj.UndoConflict) as caught:
        mj.undo(delete["id"], OWNER)
    assert caught.value.code in ("duplicate", "later_change", "changed_elsewhere")
    assert len([v for v in instincts._load_raw(OWNER).values()]) == 1

    notes.write_note(OWNER, "Notes/igual.md", "# Igual\ncontenido\n")
    notes.delete_note(OWNER, "Notes/igual.md")
    notes.write_note(OWNER, "Notes/igual.md", "# Igual\ncontenido\n")
    delete = [c for c in mj.list_changes(OWNER, store="notes") if c["op"] == "delete"][0]
    with pytest.raises(mj.UndoConflict) as caught:
        mj.undo(delete["id"], OWNER)
    assert caught.value.code == "duplicate"
    assert not os.path.exists(vault.abs_path(OWNER, "Notes/igual 2.md"))


def test_a_note_gone_from_the_trash_explains_instead_of_crashing(data):
    notes.write_note(OWNER, "Notes/perdida.md", "# Perdida\nx\n")
    notes.delete_note(OWNER, "Notes/perdida.md")
    delete = mj.list_changes(OWNER, store="notes")[0]
    with bdb.db() as conn:
        conn.execute("DELETE FROM trash")
    with pytest.raises(mj.UndoGone) as caught:
        mj.undo(delete["id"], OWNER)
    assert caught.value.code == "trash_gone"


def test_the_refusal_names_the_later_change_to_undo_first(data):
    mgr = memmod.MemoryManager(str(data))
    mgr.save([mgr.add_entry("uno", owner=OWNER)])
    entries = mgr.load_all_for_update()
    entries[0]["text"] = "dos"
    mgr.save(entries)
    entries[0]["text"] = "tres"
    mgr.save(entries)
    newer, older, _ = mj.list_changes(OWNER, store="facts")
    with pytest.raises(mj.UndoConflict) as caught:
        mj.undo(older["id"], OWNER)
    exc = caught.value
    assert exc.code == "later_change" and exc.data["blocking"]["id"] == newer["id"]
    assert "tres" in str(exc) and "edit" in str(exc)
    # a change made outside the journal says so instead
    entries = mgr.load_all_for_update()
    entries[0]["text"] = "a mano"
    with mj.suppressed():
        mgr.save(entries)
    with pytest.raises(mj.UndoConflict) as caught:
        mj.undo(newer["id"], OWNER)
    assert caught.value.code == "changed_elsewhere"


# -- P3: turn attribution --------------------------------------------------------------------

def test_a_background_extractor_is_filed_under_the_turn_that_triggered_it(data):
    mgr = memmod.MemoryManager(str(data))
    with mj.attribution(session_id="sess-1", source="memory_extractor", turn_id="sess-1:1700000000000"):
        mgr.save([mgr.add_entry("lo aprendió el extractor", owner=OWNER)])
    (change,) = mj.list_changes(OWNER, turn_id="sess-1:1700000000000")
    assert (change["source"], change["session_id"]) == ("memory_extractor", "sess-1")
    # turn id alone is enough to know the session
    with mj.attribution(turn_id="sess-2:1700000000001", source="instinct_extractor"):
        mgr.save(mgr.load_all_for_update() + [mgr.add_entry("otro", owner=OWNER)])
    assert mj.list_changes(OWNER, session_id="sess-2")[0]["turn_id"] == "sess-2:1700000000001"


def test_post_turn_extractors_receive_the_turn_id_from_the_saved_metrics():
    import inspect
    from routes import chat_helpers
    source = inspect.getsource(chat_helpers)
    assert 'turn_id=(last_metrics or {}).get("turn_id")' in source
    assert 'turn_id=md.get("turn_id")' in source
    from src import agent_loop
    assert 'metrics["turn_id"] = _context_turn_id' in inspect.getsource(agent_loop)


# -- MCP tool: catalogued, owner-scoped ---------------------------------------------------

def test_memory_changes_is_catalogued_list_reads_undo_is_a_destructive_write():
    from src.tool_capabilities import (
        KNOWN_CAPABILITY_TOOLS, POST_EXTERNAL_BLOCKED_EFFECTS, ResultIntegrity, ToolEffect, ToolRunSecurityContext,
        capabilities_for_action, capabilities_for_tool,
    )
    name = "mcp__memory__memory_changes"
    assert name in KNOWN_CAPABILITY_TOOLS and capabilities_for_tool(name).known
    listing = capabilities_for_action(name, json.dumps({"action": "list"}))
    assert listing.known and listing.effects == {ToolEffect.READ_PRIVATE}
    assert listing.result_integrity == ResultIntegrity.EXTERNAL_UNTRUSTED
    undo = capabilities_for_action(name, {"action": "undo", "change_id": "x"})
    assert {ToolEffect.WRITE_PRIVATE, ToolEffect.DESTRUCTIVE} <= undo.effects
    assert ToolEffect.READ_PRIVATE not in undo.effects or undo.effects & POST_EXTERNAL_BLOCKED_EFFECTS
    assert undo.effects & POST_EXTERNAL_BLOCKED_EFFECTS
    # anything else fails high instead of reading as a list
    other = capabilities_for_action(name, {"action": "wipe"})
    assert ToolEffect.WRITE_PRIVATE in other.effects
    # after outside content has been read, undo needs the person's go-ahead
    gate = ToolRunSecurityContext(external_untrusted_context_seen=True, external_sources=["web_fetch"])
    assert gate.decision_for(name, {"action": "undo", "change_id": "x"}).allowed is False


def test_mcp_only_sees_and_undoes_its_own_owners_changes(data, monkeypatch):
    pytest.importorskip("mcp")
    import mcp_servers.memory_server as srv
    mgr = memmod.MemoryManager(str(data))
    mgr.save([mgr.add_entry("de alice", owner=OWNER)])
    mgr.save(mgr.load_all_for_update() + [mgr.add_entry("de bob", owner="bob")])
    monkeypatch.setenv("ODYSSEUS_MCP_MEMORY_OWNER", OWNER)
    listed = json.loads(asyncio.run(srv.call_tool("memory_changes", {"action": "list"}))[0].text)
    assert [c["label"] for c in listed["changes"]] == ["de alice"]
    bob_change = mj.list_changes("bob")[0]
    out = asyncio.run(srv.call_tool("memory_changes", {"action": "undo", "change_id": bob_change["id"]}))[0].text
    assert out.startswith("Error (unknown)")
    assert [e["text"] for e in mgr.load_all_for_update()] == ["de alice", "de bob"]
    monkeypatch.setenv("ODYSSEUS_MCP_MEMORY_OWNER", "bob")
    listed = json.loads(asyncio.run(srv.call_tool("memory_changes", {"action": "list"}))[0].text)
    assert [c["label"] for c in listed["changes"]] == ["de bob"]
    # with no owner configured, owner-scoped rows are not listed at all
    monkeypatch.delenv("ODYSSEUS_MCP_MEMORY_OWNER", raising=False)
    monkeypatch.delenv("ODYSSEUS_MEMORY_OWNER", raising=False)
    listed = json.loads(asyncio.run(srv.call_tool("memory_changes", {"action": "list"}))[0].text)
    assert listed["count"] == 0
    tool = next(t for t in asyncio.run(srv.list_tools()) if t.name == "memory_changes")
    assert "needs approval" in tool.description and "read only" in tool.description
