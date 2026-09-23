"""Integration tests for `src/brain/vault.py`: two-way sync between the
vault and the stores it mirrors (memory_engine, memory.json, and — via a
fake module, since Lot B is not in this worktree — entities).

Fixtures follow tests/test_memory_grounding.py's pattern: memory_engine's
own DATA_DIR is monkeypatched per test, `db.use_dir` points brain.db at the
same tmp_path, and a fresh `MemoryManager(tmp_path)` reads/writes the same
memory.json the vault code under test uses (it resolves the personal data
dir dynamically from `src.constants.DATA_DIR`, so patching that constant is
enough for both sides to agree).
"""

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
from src.brain import vault

OWNER = "luis"


@pytest.fixture()
def brain(tmp_path, monkeypatch):
    bdb.use_dir(str(tmp_path))
    monkeypatch.setattr(engine, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    engine.set_vector_store(None)
    engine.clear_injected()
    # Lot A owns none of these stores' data — keep every test's Home/Project
    # export from touching the real, process-wide projects.json.
    monkeypatch.setattr(vault, "_projects_for_owner", lambda owner: [])
    yield tmp_path
    engine.reset_vector_store()
    engine.clear_injected()
    bdb.use_dir(None)


@pytest.fixture()
def personal_mgr(tmp_path):
    return memmod.MemoryManager(str(tmp_path))


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _all_files(root: str):
    out = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            out.append(os.path.join(dirpath, name))
    return out


# ── fake entities module (Lot B stub, per the contract) ─────────────────

@pytest.fixture()
def fake_entities(monkeypatch):
    store: dict = {}

    def upsert(id_, **fields):
        store.setdefault(id_, {
            "id": id_, "owner": OWNER, "project": "", "name": id_, "type": "person",
            "aliases": [], "summary": "", "hidden": False,
            "created_at": "2025-01-01T00:00:00Z", "updated_at": "2025-01-01T00:00:00Z",
        })
        store[id_].update(fields)
        return store[id_]

    def get_entity(id_):
        return store.get(id_)

    def list_entities(owner="", q="", type="", limit=200, include_hidden=False):  # noqa: A002
        return [e for e in store.values() if include_hidden or not e.get("hidden")]

    def update_entity(id_, **fields):
        store[id_].update(fields)
        return store[id_]

    def set_hidden(id_, hidden):
        store[id_]["hidden"] = bool(hidden)
        return store[id_]

    def mentions_for(source_ref):
        return [e for e in store.values() if source_ref in e.get("_mentions", [])]

    def profile(entity_id, as_of=None):
        e = store[entity_id]
        return {
            "entity": e, "facts": [], "relations": [], "history": [], "timeline": [],
            "summary": e.get("summary", ""), "summary_sources": [],
        }

    fake = types.ModuleType("src.brain.entities")
    fake.upsert = upsert
    fake.get_entity = get_entity
    fake.list_entities = list_entities
    fake.update_entity = update_entity
    fake.set_hidden = set_hidden
    fake.mentions_for = mentions_for
    fake.profile = profile
    monkeypatch.setitem(sys.modules, "src.brain.entities", fake)
    return store


# ── round-trip stability ─────────────────────────────────────────────────

def test_sync_twice_with_no_changes_writes_nothing_and_imports_nothing(brain):
    engine.add_item("Ada prefers dark mode in the editor", owner=OWNER, type="preference")
    engine.add_item("Bruno reviews every pull request", owner=OWNER, type="fact")

    first = vault.sync(OWNER)
    assert first["exported"] > 0

    root = vault.vault_root(OWNER)
    before = {p: os.stat(p).st_mtime_ns for p in _all_files(root)}

    second = vault.sync(OWNER)
    assert second["exported"] == 0
    assert second["imported"] == 0
    assert second["created"] == 0
    assert second["suppressed"] == 0
    assert second["guard_tripped"] is False

    after = {p: os.stat(p).st_mtime_ns for p in _all_files(root)}
    assert before == after  # not one file was rewritten


def test_render_of_an_unchanged_item_is_byte_identical_across_calls(brain):
    item = engine.add_item("Cordera Labs uses a four-day work week", owner=OWNER, type="fact")
    fresh = engine.get_item(item["id"])
    source1, path1, render_fn1 = vault._memory_target(fresh)
    source2, path2, render_fn2 = vault._memory_target(fresh)
    assert (source1, path1) == (source2, path2)
    assert render_fn1("same zone") == render_fn2("same zone")


# ── path safety ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", [
    "../escape.md", "/abs/path.md", "a/../../b.md", "notes.txt", ".trash/x.md", ".trash",
    "", "   ",
])
def test_validate_path_rejects_unsafe_paths(brain, bad):
    with pytest.raises(vault.VaultPathError):
        vault.validate_path(bad)


def test_validate_path_normalizes_backslashes_and_dot_segments(brain):
    assert vault.validate_path("Notes/./Sub\\Thing.md") == "Notes/Sub/Thing.md"


# ── secret / suppressed items never reach disk ───────────────────────────

def test_secret_memory_is_never_written(brain):
    secret = engine.add_item("The vault password is hunter2", owner=OWNER, sensitivity="secret")
    engine.add_item("Bruno leads the Cordera Labs project", owner=OWNER)
    vault.sync(OWNER)
    root = vault.vault_root(OWNER)
    for path in _all_files(root):
        assert "hunter2" not in _read(path)
    with bdb.db() as conn:
        rows = conn.execute("SELECT source FROM sync_state WHERE owner=?", (OWNER,)).fetchall()
    assert f"mem:{secret['id']}" not in {r["source"] for r in rows}


def test_suppressed_memory_is_not_exported(brain):
    item = engine.add_item("Old fact nobody needs", owner=OWNER)
    engine.set_suppressed(item["id"], True)
    engine.add_item("Fresh fact", owner=OWNER)
    report = vault.sync(OWNER)
    root = vault.vault_root(OWNER)
    for path in _all_files(root):
        assert "Old fact nobody needs" not in _read(path)


def test_forgotten_and_non_active_status_excluded(brain):
    procedure = engine.add_item("Deploy with the staging script", owner=OWNER, level="procedural")
    engine.forget(procedure["id"], reason="obsolete")
    vault.sync(OWNER)
    root = vault.vault_root(OWNER)
    for path in _all_files(root):
        assert "Deploy with the staging script" not in _read(path)


# ── import: editing a memory note's text renames the file (new id) ─────

def test_editing_memory_text_corrects_and_renames(brain):
    item = engine.add_item("Ada prefers dark mode", owner=OWNER, type="preference")
    vault.sync(OWNER)
    old_path = f"Memories/Preferences/Ada prefers dark mode ({item['id'][:8]}).md"
    full = vault.abs_path(OWNER, old_path)
    fm_data, body = fm.split(_read(full))
    new_body = body.replace("Ada prefers dark mode", "Ada prefers light mode")
    vault._atomic_write(full, fm.join(fm_data, new_body))

    report = vault.sync(OWNER)
    assert report["imported"] >= 1
    assert not os.path.exists(full)  # the old, id-suffixed file is gone

    old_item = engine.get_item(item["id"])
    assert old_item is None or old_item.get("status") == "forgotten"

    items = [i for i in engine.list_items(owner=OWNER, limit=50) if i["status"] == "active"]
    assert len(items) == 1
    assert items[0]["text"] == "Ada prefers light mode"
    assert items[0]["provenance"].get("corrected_from") == item["id"]

    new_path = f"Memories/Preferences/Ada prefers light mode ({items[0]['id'][:8]}).md"
    assert os.path.exists(vault.abs_path(OWNER, new_path))


def test_frontmatter_only_edit_applies_fields_without_renaming(brain):
    item = engine.add_item("Stable fact text", owner=OWNER, type="fact", confidence=0.5)
    vault.sync(OWNER)
    path = f"Memories/Facts/Stable fact text ({item['id'][:8]}).md"
    full = vault.abs_path(OWNER, path)
    fm_data, body = fm.split(_read(full))
    fm_data["pinned"] = True
    fm_data["confidence"] = 0.91
    fm_data["valid_until"] = "2030-01-01T00:00:00Z"
    vault._atomic_write(full, fm.join(fm_data, body))

    report = vault.sync(OWNER)
    assert report["imported"] == 1
    assert os.path.exists(full)  # same file: text did not change, no rename

    updated = engine.get_item(item["id"])
    assert updated["pinned"] is True
    assert abs(updated["confidence"] - 0.91) < 1e-6
    assert updated["valid_until"].startswith("2030-01-01")


def test_new_file_under_memories_without_source_becomes_human_explicit_memory(brain):
    rel = "Memories/Facts/A brand new fact.md"
    full = vault.abs_path(OWNER, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    vault._atomic_write(full, "---\nkind: note\n---\n\nCordera Labs uses a four-day week.\n")

    report = vault.sync(OWNER)
    assert report["created"] == 1
    assert not os.path.exists(full)  # replaced by the canonical, id-suffixed file

    items = engine.list_items(owner=OWNER, limit=10)
    assert len(items) == 1
    assert items[0]["text"] == "Cordera Labs uses a four-day week."
    assert items[0]["trust_class"] == "human_explicit"
    assert items[0]["type"] == "fact"  # inferred from the Facts/ folder


def test_new_memory_file_in_unrecognized_folder_defaults_to_fact_type(brain):
    rel = "Memories/A loose note.md"
    full = vault.abs_path(OWNER, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    vault._atomic_write(full, "Just some text, no frontmatter at all.\n")
    report = vault.sync(OWNER)
    assert report["created"] == 1
    items = engine.list_items(owner=OWNER, limit=10)
    assert items[0]["type"] == "fact"


# ── import: personal (memory.json) notes ─────────────────────────────────

def test_editing_personal_note_text_updates_memory_json(brain, personal_mgr):
    entries = [personal_mgr.add_entry("Loves espresso in the morning", owner=OWNER)]
    personal_mgr.save(entries)
    vault.sync(OWNER)

    entry_id = entries[0]["id"]
    rel = f"Personal/Loves espresso in the morning ({entry_id[:8]}).md"
    full = vault.abs_path(OWNER, rel)
    fm_data, body = fm.split(_read(full))
    new_body = body.replace("Loves espresso in the morning", "Loves tea in the morning")
    vault._atomic_write(full, fm.join(fm_data, new_body))

    report = vault.sync(OWNER)
    assert report["imported"] == 1
    reloaded = personal_mgr.load(OWNER)
    assert reloaded[0]["text"] == "Loves tea in the morning"
    assert reloaded[0]["id"] == entry_id  # personal entries keep a stable id


def test_missing_personal_file_removes_the_entry_and_trashes_it(brain, personal_mgr):
    entries = [personal_mgr.add_entry("Temporary personal note", owner=OWNER)]
    personal_mgr.save(entries)
    vault.sync(OWNER)
    entry_id = entries[0]["id"]
    rel = f"Personal/Temporary personal note ({entry_id[:8]}).md"
    os.remove(vault.abs_path(OWNER, rel))

    report = vault.sync(OWNER)
    assert report["suppressed"] == 1
    assert personal_mgr.load(OWNER) == []

    with bdb.db() as conn:
        row = conn.execute(
            "SELECT effect, content FROM trash WHERE owner=? AND source=?",
            (OWNER, f"pmem:{entry_id}"),
        ).fetchone()
    assert row["effect"] == "removed"
    assert "Temporary personal note" in row["content"]


# ── deletion of a memory file: suppress + trash ──────────────────────────

def test_deleting_a_memory_file_suppresses_it_and_records_trash(brain):
    item = engine.add_item("Fact to be deleted from disk", owner=OWNER)
    vault.sync(OWNER)
    rel = f"Memories/Facts/Fact to be deleted from disk ({item['id'][:8]}).md"
    os.remove(vault.abs_path(OWNER, rel))

    report = vault.sync(OWNER)
    assert report["suppressed"] == 1
    assert engine.get_item(item["id"])["suppressed"] is True

    with bdb.db() as conn:
        row = conn.execute(
            "SELECT effect FROM trash WHERE owner=? AND source=?", (OWNER, f"mem:{item['id']}")
        ).fetchone()
    assert row["effect"] == "suppressed"

    # a further sync must not re-suppress or re-trash the same item
    report2 = vault.sync(OWNER)
    assert report2["suppressed"] == 0


# ── the mass-deletion guard ───────────────────────────────────────────────

def test_guard_trips_on_mass_deletion_and_touches_nothing(brain):
    items = [engine.add_item(f"Guarded fact {i}", owner=OWNER) for i in range(10)]
    vault.sync(OWNER)
    facts_dir = os.path.join(vault.vault_root(OWNER), "Memories", "Facts")
    files = sorted(os.listdir(facts_dir))
    for name in files[:6]:  # 6 of 10 > max(5, 0.3*10)
        os.remove(os.path.join(facts_dir, name))

    report = vault.sync(OWNER)
    assert report["guard_tripped"] is True
    assert report["suppressed"] == 0
    # the guard also stops the export phase from silently recreating them
    assert len(os.listdir(facts_dir)) == 4
    for item in items:
        assert engine.get_item(item["id"])["suppressed"] is False


def test_guard_does_not_trip_below_threshold(brain):
    items = [engine.add_item(f"Small deletion fact {i}", owner=OWNER) for i in range(10)]
    vault.sync(OWNER)
    facts_dir = os.path.join(vault.vault_root(OWNER), "Memories", "Facts")
    files = sorted(os.listdir(facts_dir))
    os.remove(os.path.join(facts_dir, files[0]))  # 1 of 10, well under threshold

    report = vault.sync(OWNER)
    assert report["guard_tripped"] is False
    assert report["suppressed"] == 1


# ── entities (via the fake Lot B module) ─────────────────────────────────

def test_entity_export_uses_fake_entities_module(brain, fake_entities):
    fake_entities["e1"] = {
        "id": "e1", "owner": OWNER, "project": "", "name": "Ada", "type": "person",
        "aliases": ["Ada L."], "summary": "An engineer.", "hidden": False,
        "created_at": "2025-01-01T00:00:00Z", "updated_at": "2025-01-01T00:00:00Z",
    }
    report = vault.sync(OWNER)
    assert report["errors"] == []
    path = vault.abs_path(OWNER, "Entities/Person/Ada.md")
    assert os.path.exists(path)
    assert "An engineer." in _read(path)


def test_missing_entity_file_hides_it_and_trashes(brain, fake_entities):
    fake_entities["e1"] = {
        "id": "e1", "owner": OWNER, "project": "", "name": "Ada", "type": "person",
        "aliases": [], "summary": "", "hidden": False,
        "created_at": "2025-01-01T00:00:00Z", "updated_at": "2025-01-01T00:00:00Z",
    }
    vault.sync(OWNER)
    path = vault.abs_path(OWNER, "Entities/Person/Ada.md")
    os.remove(path)
    report = vault.sync(OWNER)
    assert report["suppressed"] == 1
    assert fake_entities["e1"]["hidden"] is True


def test_entity_facts_are_cited_not_rendered_as_broken_wikilinks(brain, fake_entities, monkeypatch):
    fake_entities["e1"] = {
        "id": "e1", "owner": OWNER, "project": "", "name": "Ada", "type": "person",
        "aliases": [], "summary": "", "hidden": False,
        "created_at": "2025-01-01T00:00:00Z", "updated_at": "2025-01-01T00:00:00Z",
    }
    import sys
    fake_mod = sys.modules["src.brain.entities"]
    monkeypatch.setattr(fake_mod, "profile", lambda entity_id, as_of=None: {
        "entity": fake_entities[entity_id],
        "facts": [{"source_ref": "mem:abcdef1234567890", "text": "Works at Cordera Labs"}],
        "relations": [], "history": [], "timeline": [], "summary": "", "summary_sources": [],
    })
    vault.sync(OWNER)
    content = _read(vault.abs_path(OWNER, "Entities/Person/Ada.md"))
    assert "Works at Cordera Labs" in content
    assert "`mem:abcdef12`" in content
    assert "[[abcdef12]]" not in content  # no unresolvable id-only wikilink


def test_entities_module_absent_degrades_gracefully(brain):
    # Lot B is genuinely not importable in this worktree: sync must not error.
    report = vault.sync(OWNER)
    assert report["errors"] == []
    assert not os.path.isdir(os.path.join(vault.vault_root(OWNER), "Entities"))


# ── atomic, Windows-safe, BOM-free writes ────────────────────────────────

def test_writes_are_utf8_no_bom_and_lf_newlines(brain):
    engine.add_item("Café con Bruno en Móstoles", owner=OWNER)
    vault.sync(OWNER)
    root = vault.vault_root(OWNER)
    found = False
    for path in _all_files(root):
        with open(path, "rb") as fh:
            raw = fh.read()
        assert not raw.startswith(b"\xef\xbb\xbf"), f"{path} has a BOM"
        assert b"\r\n" not in raw, f"{path} has CRLF newlines"
        if b"Bruno" in raw:
            found = True
    assert found


def test_filenames_are_windows_safe(brain):
    engine.add_item('A fact with <bad> chars: "quoted" | pipe? * star', owner=OWNER)
    vault.sync(OWNER)
    root = vault.vault_root(OWNER)
    for path in _all_files(root):
        name = os.path.basename(path)
        assert not any(ch in name for ch in '<>:"|?*')


def test_no_leftover_tmp_files_after_sync(brain):
    engine.add_item("Something to export", owner=OWNER)
    vault.sync(OWNER)
    root = vault.vault_root(OWNER)
    for path in _all_files(root):
        assert ".tmp" not in os.path.basename(path)
