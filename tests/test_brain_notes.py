"""Tests for `src/brain/notes.py`: tree, read/write/create/rename/delete,
search, graph, tags, backlinks and the daily note."""

from __future__ import annotations

import os
import sys
import types

import pytest

from src import constants
from src import memory as memmod
from src import memory_engine as engine
from src.brain import db as bdb
from src.brain import frontmatter as fm
from src.brain import notes
from src.brain import vault

OWNER = "luis"


@pytest.fixture()
def brain(tmp_path, monkeypatch):
    bdb.use_dir(str(tmp_path))
    monkeypatch.setattr(engine, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    engine.set_vector_store(None)
    engine.clear_injected()
    monkeypatch.setattr(vault, "_projects_for_owner", lambda owner: [])
    yield tmp_path
    engine.reset_vector_store()
    engine.clear_injected()
    bdb.use_dir(None)


@pytest.fixture()
def fake_entities(monkeypatch):
    store: dict = {}
    fake = types.ModuleType("src.brain.entities")
    fake.get_entity = lambda id_: store.get(id_)
    fake.list_entities = lambda owner="", q="", type="", limit=200, include_hidden=False: [
        e for e in store.values() if include_hidden or not e.get("hidden")
    ]
    fake.update_entity = lambda id_, **f: (store[id_].update(f), store[id_])[1]
    fake.set_hidden = lambda id_, hidden: (store[id_].update(hidden=bool(hidden)), store[id_])[1]
    fake.mentions_for = lambda source_ref: []
    fake.profile = lambda entity_id, as_of=None: {
        "entity": store[entity_id], "facts": [], "relations": [], "history": [],
        "timeline": [], "summary": store[entity_id].get("summary", ""), "summary_sources": [],
    }
    monkeypatch.setitem(sys.modules, "src.brain.entities", fake)
    return store


# ── tree / read ──────────────────────────────────────────────────────────

def test_tree_lists_folders_and_notes(brain):
    engine.add_item("A fact worth keeping", owner=OWNER)
    vault.sync(OWNER)
    t = notes.tree(OWNER)
    paths = {n["path"] for n in t["notes"]}
    assert "Home.md" in paths
    assert any(p.startswith("Memories/Facts/") for p in paths)
    assert "Memories/Facts" in t["folders"]
    assert "Memories" in t["folders"]


def test_read_note_shape(brain):
    item = engine.add_item("Ada prefers dark mode", owner=OWNER, type="preference")
    vault.sync(OWNER)
    path = f"Memories/Preferences/Ada prefers dark mode ({item['id'][:8]}).md"
    note = notes.read_note(OWNER, path)
    assert note["path"] == path
    assert note["kind"] == "memory"
    assert note["source"] == f"mem:{item['id']}"
    assert note["user_zone"] == "Ada prefers dark mode"
    assert "## Entities" in note["generated"]
    assert note["editable"] is True
    assert isinstance(note["links"], list)
    assert isinstance(note["backlinks"], list)


def test_read_note_missing_raises(brain):
    with pytest.raises(FileNotFoundError):
        notes.read_note(OWNER, "Notes/does-not-exist.md")


def test_notes_reject_unsafe_paths(brain):
    for fn, args in [
        (notes.read_note, (OWNER, "../x.md")),
        (notes.write_note, (OWNER, "/abs.md", "text")),
        (notes.delete_note, (OWNER, "notes.txt")),
        (notes.backlinks, (OWNER, ".trash/x.md")),
    ]:
        with pytest.raises(vault.VaultPathError):
            fn(*args)


# ── write / create ────────────────────────────────────────────────────────

def test_write_note_free_note_round_trips(brain):
    created = notes.create_note(OWNER, "My Idea", folder="Notes", content="Initial text.")
    result = notes.write_note(OWNER, created["path"], "---\nkind: note\n---\n\nEdited text.\n")
    assert result["note"]["user_zone"] == "Edited text."
    assert result["applied"]["action"] == "noop"  # free notes have no store to import into


def test_write_note_on_memory_updates_text_immediately(brain):
    item = engine.add_item("Original memory text", owner=OWNER)
    vault.sync(OWNER)
    path = f"Memories/Facts/Original memory text ({item['id'][:8]}).md"
    note = notes.read_note(OWNER, path)
    new_content = note["content"].replace("Original memory text", "Updated memory text")
    result = notes.write_note(OWNER, path, new_content)
    assert result["applied"]["action"] == "updated"
    assert result["note"]["user_zone"] == "Updated memory text"
    assert result["note"]["path"] != path  # renamed to the corrected item's id
    updated_item = engine.get_item(result["applied"]["source"].split(":", 1)[1])
    assert updated_item["text"] == "Updated memory text"


def test_create_note_default_folder_and_frontmatter(brain):
    note = notes.create_note(OWNER, "Loose Thought")
    assert note["path"] == "Notes/Loose Thought.md"
    assert note["kind"] == "note"
    assert note["frontmatter"]["kind"] == "note"


def test_create_note_avoids_clobbering_an_existing_file(brain):
    first = notes.create_note(OWNER, "Duplicate", folder="Notes", content="first")
    second = notes.create_note(OWNER, "Duplicate", folder="Notes", content="second")
    assert first["path"] != second["path"]
    assert notes.read_note(OWNER, first["path"])["user_zone"] == "first"
    assert notes.read_note(OWNER, second["path"])["user_zone"] == "second"


def test_create_note_under_memories_without_source_becomes_a_memory(brain):
    note = notes.create_note(OWNER, "A quick fact", folder="Memories/Facts", content="Quick fact text.")
    assert note["kind"] == "memory"
    assert note["source"].startswith("mem:")
    items = engine.list_items(owner=OWNER, limit=10)
    assert items[0]["text"] == "Quick fact text."


# ── rename ───────────────────────────────────────────────────────────────

def test_rename_memory_note_is_refused(brain):
    item = engine.add_item("Cannot rename this", owner=OWNER)
    vault.sync(OWNER)
    path = f"Memories/Facts/Cannot rename this ({item['id'][:8]}).md"
    with pytest.raises(ValueError):
        notes.rename_note(OWNER, path, "New Title")


def test_rename_free_note_updates_links_including_piped_and_heading_forms(brain):
    target = notes.create_note(OWNER, "Villanueva", folder="Notes", content="A town.")
    notes.create_note(
        OWNER, "Trip A", folder="Notes",
        content="We visited [[Villanueva]] last week.",
    )
    notes.create_note(
        OWNER, "Trip B", folder="Notes",
        content="See [[Villanueva|the town]] and [[Villanueva#history|its story]].",
    )

    result = notes.rename_note(OWNER, target["path"], "Villanueva del Rio")
    assert result["note"]["path"] == "Notes/Villanueva del Rio.md"
    assert result["updated_links"] == 2

    trip_a = notes.read_note(OWNER, "Notes/Trip A.md")
    assert "[[Villanueva del Rio]]" in trip_a["content"]
    trip_b = notes.read_note(OWNER, "Notes/Trip B.md")
    assert "[[Villanueva del Rio|the town]]" in trip_b["content"]
    assert "[[Villanueva del Rio#history|its story]]" in trip_b["content"]


def test_rename_note_refuses_collision(brain):
    notes.create_note(OWNER, "Alpha", folder="Notes")
    beta = notes.create_note(OWNER, "Beta", folder="Notes")
    with pytest.raises(ValueError):
        notes.rename_note(OWNER, beta["path"], "Alpha")


def test_rename_entity_note_via_fake_module(brain, fake_entities):
    fake_entities["e1"] = {
        "id": "e1", "owner": OWNER, "project": "", "name": "Ada", "type": "person",
        "aliases": [], "summary": "", "hidden": False,
        "created_at": "x", "updated_at": "x",
    }
    vault.sync(OWNER)
    result = notes.rename_note(OWNER, "Entities/Person/Ada.md", "Ada Lovelace")
    assert result["note"]["path"] == "Entities/Person/Ada Lovelace.md"
    assert fake_entities["e1"]["name"] == "Ada Lovelace"


# ── delete / trash / restore ─────────────────────────────────────────────

def test_delete_memory_note_suppresses_and_trashes(brain):
    item = engine.add_item("To be deleted via API", owner=OWNER)
    vault.sync(OWNER)
    path = f"Memories/Facts/To be deleted via API ({item['id'][:8]}).md"
    result = notes.delete_note(OWNER, path)
    assert result["effect"] == "suppressed"
    assert engine.get_item(item["id"])["suppressed"] is True
    trashed = notes.list_trash(OWNER)
    assert any(t["id"] == result["trash_id"] for t in trashed)
    assert not os.path.exists(vault.abs_path(OWNER, path))


def test_restore_memory_note_from_trash(brain):
    item = engine.add_item("Restorable memory", owner=OWNER)
    vault.sync(OWNER)
    path = f"Memories/Facts/Restorable memory ({item['id'][:8]}).md"
    result = notes.delete_note(OWNER, path)
    restored = notes.restore(OWNER, result["trash_id"])
    assert restored["source"] == f"mem:{item['id']}"
    assert engine.get_item(item["id"])["suppressed"] is False
    assert os.path.exists(vault.abs_path(OWNER, restored["path"]))
    remaining_trash = notes.list_trash(OWNER)
    assert result["trash_id"] not in {t["id"] for t in remaining_trash}


def test_delete_and_restore_a_free_note(brain):
    note = notes.create_note(OWNER, "Ephemeral", folder="Notes", content="content here")
    result = notes.delete_note(OWNER, note["path"])
    assert result["effect"] == "removed"
    assert not os.path.exists(vault.abs_path(OWNER, note["path"]))
    restored = notes.restore(OWNER, result["trash_id"])
    assert restored["path"] == note["path"]
    assert "content here" in restored["content"]


def test_delete_personal_note_removes_from_memory_json(brain):
    mgr = memmod.MemoryManager(str(brain))
    entries = [mgr.add_entry("Personal fact to delete", owner=OWNER)]
    mgr.save(entries)
    vault.sync(OWNER)
    entry_id = entries[0]["id"]
    path = f"Personal/Personal fact to delete ({entry_id[:8]}).md"
    result = notes.delete_note(OWNER, path)
    assert result["effect"] == "removed"
    assert mgr.load(OWNER) == []


# ── search ────────────────────────────────────────────────────────────────

def test_search_is_accent_insensitive_with_title_boost(brain):
    engine.add_item("Fact about Móstoles and its history", owner=OWNER, category="a")
    engine.add_item("Unrelated fact that only briefly mentions Mostoles in passing", owner=OWNER, category="b")
    notes.create_note(OWNER, "Mostoles", folder="Notes", content="A note whose title IS the place.")
    vault.sync(OWNER)

    results = notes.search(OWNER, "mostoles")
    assert results, "accent-insensitive search should find matches"
    paths = [r["path"] for r in results]
    assert "Notes/Mostoles.md" in paths
    # the note whose TITLE matches should outrank the ones matching only in body
    assert paths.index("Notes/Mostoles.md") == 0


def test_search_empty_query_returns_nothing(brain):
    assert notes.search(OWNER, "") == []
    assert notes.search(OWNER, "   ") == []


def test_search_respects_limit(brain):
    for i in range(5):
        notes.create_note(OWNER, f"Widget {i}", folder="Notes", content="widget content")
    results = notes.search(OWNER, "widget", limit=2)
    assert len(results) == 2


# ── graph / tags / unresolved / backlinks ────────────────────────────────

def test_graph_global_and_unresolved_nodes(brain):
    a = notes.create_note(OWNER, "Alpha", folder="Notes", content="Links to [[Beta]] and [[Ghost]].")
    b = notes.create_note(OWNER, "Beta", folder="Notes", content="No links here.")
    g = notes.graph(OWNER)
    node_ids = {n["id"] for n in g["nodes"]}
    assert a["path"] in node_ids and b["path"] in node_ids
    unresolved_nodes = [n for n in g["nodes"] if n["kind"] == "unresolved"]
    assert any(n["label"] == "Ghost" for n in unresolved_nodes)
    edge_pairs = {(e["from"], e["to"]) for e in g["edges"]}
    assert (a["path"], b["path"]) in edge_pairs


def test_graph_local_mode_center_and_depth(brain):
    a = notes.create_note(OWNER, "Hub", folder="Notes", content="[[Leaf1]] [[Leaf2]]")
    notes.create_note(OWNER, "Leaf1", folder="Notes", content="[[FarAway]]")
    notes.create_note(OWNER, "Leaf2", folder="Notes", content="no links")
    notes.create_note(OWNER, "FarAway", folder="Notes", content="no links")

    g1 = notes.graph(OWNER, center=a["path"], depth=1)
    ids1 = {n["id"] for n in g1["nodes"]}
    assert "Notes/FarAway.md" not in ids1
    assert "Notes/Leaf1.md" in ids1

    g2 = notes.graph(OWNER, center=a["path"], depth=2)
    ids2 = {n["id"] for n in g2["nodes"]}
    assert "Notes/FarAway.md" in ids2


def test_graph_kind_filter(brain):
    engine.add_item("A memory for the graph", owner=OWNER)
    notes.create_note(OWNER, "Plain note", folder="Notes")
    vault.sync(OWNER)
    g = notes.graph(OWNER, kinds=["memory"])
    kinds = {n["kind"] for n in g["nodes"]}
    assert kinds <= {"memory"}


def test_tags_counted_across_notes(brain):
    notes.create_note(OWNER, "One", folder="Notes", content="About #proyecto and #urgente.")
    notes.create_note(OWNER, "Two", folder="Notes", content="Also #proyecto related.")
    counts = {t["tag"]: t["count"] for t in notes.tags(OWNER)}
    assert counts["proyecto"] == 2
    assert counts["urgente"] == 1


def test_unresolved_links_grouped_by_target(brain):
    notes.create_note(OWNER, "One", folder="Notes", content="See [[Nowhere]].")
    notes.create_note(OWNER, "Two", folder="Notes", content="Also see [[Nowhere]].")
    result = notes.unresolved(OWNER)
    assert len(result) == 1
    assert result[0]["target"] == "Nowhere"
    assert set(result[0]["from"]) == {"Notes/One.md", "Notes/Two.md"}


def test_backlinks_include_a_short_context_line(brain):
    target = notes.create_note(OWNER, "Referenced", folder="Notes", content="content")
    notes.create_note(OWNER, "Source", folder="Notes", content="A sentence mentioning [[Referenced]] here.")
    bl = notes.backlinks(OWNER, target["path"])
    assert len(bl) == 1
    assert bl[0]["path"] == "Notes/Source.md"
    assert "[[Referenced]]" in bl[0]["context"]


# ── daily note ────────────────────────────────────────────────────────────

def test_daily_note_created_on_first_call_then_reused(brain):
    note1 = notes.daily_note(OWNER, "2025-06-01")
    assert note1["path"] == "Daily/2025-06-01.md"
    note1_edit = notes.write_note(OWNER, note1["path"], note1["content"] + "\nHand-written line.\n")
    note2 = notes.daily_note(OWNER, "2025-06-01")
    assert "Hand-written line." in note2["content"]


def test_daily_note_defaults_to_today_and_validates_date(brain):
    note = notes.daily_note(OWNER)
    assert note["path"].startswith("Daily/")
    with pytest.raises(ValueError):
        notes.daily_note(OWNER, "not-a-date")


# ── reindex ───────────────────────────────────────────────────────────────

def test_reindex_returns_count_and_cleans_stale_rows(brain):
    note = notes.create_note(OWNER, "Temp", folder="Notes", content="x")
    count_before = notes.reindex(OWNER)
    assert count_before >= 1
    os.remove(vault.abs_path(OWNER, note["path"]))
    count_after = notes.reindex(OWNER)
    assert count_after == count_before - 1
    t = notes.tree(OWNER)
    assert note["path"] not in {n["path"] for n in t["notes"]}
