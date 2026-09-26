"""Regression tests for review findings on the markdown vault sync
(`src/brain/vault.py`, `notes.py`, `render.py`, `frontmatter.py`,
`wikilinks.py`) and the `memory_engine.correct()` it relies on.

The guiding rule every test here pins down: the store is the truth for a
mirrored note; a human edit in a file is applied as a minimal, validated
patch against the version the vault last exported; nothing silently loses
or downgrades user data; anything the sync cannot interpret safely is left
on disk untouched and reported in `errors`.
"""

from __future__ import annotations

import os
import time

import pytest

from src import constants
from src import memory as memmod
from src import memory_engine as engine
from src.brain import db as bdb
from src.brain import frontmatter as fm
from src.brain import notes, render, vault, wikilinks

OWNER = "nora"


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
def ent(brain):
    from src.brain import entities
    return entities


def _root(owner: str = OWNER) -> str:
    return vault.vault_root(owner)


def _files(owner: str = OWNER):
    root = _root(owner)
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".trash"]
        for name in filenames:
            if name.endswith(".md"):
                out.append(os.path.relpath(os.path.join(dirpath, name), root).replace(os.sep, "/"))
    return sorted(out)


def _rd(rel: str, owner: str = OWNER) -> str:
    with open(os.path.join(_root(owner), rel), encoding="utf-8") as fh:
        return fh.read()


def _wr(rel: str, text: str, owner: str = OWNER, encoding: str = "utf-8") -> None:
    full = os.path.join(_root(owner), rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    # a different mtime than the vault's own write, like a human saving later
    with open(full, "w", encoding=encoding, newline="\n") as fh:
        fh.write(text)
    stamp = time.time() + 5
    os.utime(full, (stamp, stamp))


def _only(prefix: str, needle: str = "") -> str:
    hits = [f for f in _files() if f.startswith(prefix) and needle in f]
    assert len(hits) == 1, hits
    return hits[0]


def _active(owner: str = OWNER):
    return [m for m in engine.list_items(owner=owner, limit=500)
            if m["status"] == "active" and not m.get("suppressed")]


# ── finding 1: a text edit must never delete the memory ──────────────────

def test_invalid_level_in_frontmatter_is_rejected_and_later_text_edit_keeps_the_memory(brain):
    engine.add_item("Ada prefiere el te verde", owner=OWNER, trust_class="human_explicit")
    vault.sync(OWNER)
    path = _only("Memories/", "Ada")
    _wr(path, _rd(path).replace("level: semantic", "level: semantica"))
    report = vault.sync(OWNER)
    assert any("level" in e for e in report["errors"])
    assert _active()[0]["level"] == "semantic"  # nothing invalid reached the store

    path = _only("Memories/", "Ada")
    _wr(path, _rd(path).replace("Ada prefiere el te verde\n", "Ada prefiere el te rojo\n"))
    vault.sync(OWNER)
    live = _active()
    assert [m["text"] for m in live] == ["Ada prefiere el te rojo"]


def test_correct_survives_an_invalid_stored_level_and_validates_before_forgetting(brain):
    item = engine.add_item("Bruno usa tabuladores", owner=OWNER, trust_class="human_explicit")
    raw = engine.get_item(item["id"])
    raw["level"] = "semantica"  # what an older unvalidated import could store
    engine.save_item(raw)
    new = engine.correct(item["id"], "Bruno usa espacios", reason="test")
    assert new is not None and new["level"] == "semantic"
    assert engine.get_item(item["id"]) is None

    with pytest.raises(engine.MemoryEngineError):
        engine.correct(new["id"], "Bruno usa guiones", level="nonsense")
    assert engine.get_item(new["id"]) is not None  # the original is untouched
    assert not engine.is_tombstoned("Bruno usa espacios", owner=OWNER)


def test_correct_puts_the_original_back_if_the_write_fails(brain, monkeypatch):
    item = engine.add_item("Cordera Labs usa la base Orca", owner=OWNER, trust_class="human_explicit")

    def boom(*a, **kw):
        raise RuntimeError("disk full")

    real_add = engine.add_item
    monkeypatch.setattr(engine, "add_item", boom)
    with pytest.raises(RuntimeError):
        engine.correct(item["id"], "Cordera Labs usa la base Lince")
    monkeypatch.setattr(engine, "add_item", real_add)
    assert engine.get_item(item["id"])["text"] == "Cordera Labs usa la base Orca"
    assert not engine.is_tombstoned("Cordera Labs usa la base Orca", owner=OWNER)


# ── finding 2: correct() keeps what the owner decided ────────────────────

def test_correct_carries_pinned_window_sensitivity_and_suppressed(brain):
    a = engine.add_item("Bruno prefiere reuniones cortas", owner=OWNER, trust_class="human_explicit",
                        valid_until="2030-01-01T00:00:00Z", sensitivity="sensitive")
    engine.set_pinned(a["id"], True)
    engine.set_suppressed(a["id"], True)
    new = engine.correct(a["id"], "Bruno prefiere reuniones muy cortas")
    assert new["pinned"] is True
    assert new["suppressed"] is True
    assert new["sensitivity"] == "sensitive"
    assert new["valid_until"].startswith("2030-01-01")


def test_vault_text_edit_keeps_pinned_window_and_sensitivity(brain):
    a = engine.add_item("Bruno prefiere reuniones cortas", owner=OWNER, trust_class="human_explicit",
                        valid_until="2030-01-01T00:00:00Z", sensitivity="sensitive")
    engine.set_pinned(a["id"], True)
    vault.sync(OWNER)
    path = _only("Memories/", "Bruno")
    _wr(path, _rd(path).replace("Bruno prefiere reuniones cortas\n", "Bruno prefiere reuniones muy cortas\n"))
    report = vault.sync(OWNER)
    assert report["errors"] == []
    [m] = _active()
    assert m["text"] == "Bruno prefiere reuniones muy cortas"
    assert m["pinned"] is True and m["sensitivity"] == "sensitive"
    assert m["valid_until"].startswith("2030-01-01")


# ── finding 3: moving/renaming a mirrored file is not a deletion ─────────

def test_moving_a_memory_file_does_not_suppress_it(brain):
    it = engine.add_item("Bruno usa Orca para los scripts", owner=OWNER, trust_class="human_explicit")
    vault.sync(OWNER)
    src = _only("Memories/", "Bruno")
    os.makedirs(os.path.join(_root(), "Notes"), exist_ok=True)
    os.rename(os.path.join(_root(), src), os.path.join(_root(), "Notes", "bruno orca.md"))
    report = vault.sync(OWNER)
    assert report["suppressed"] == 0 and report["errors"] == []
    assert engine.get_item(it["id"])["suppressed"] is False
    assert notes.list_trash(OWNER) == []
    # the note is still there (where the human put it) and it is still the mirror
    assert "Notes/bruno orca.md" in _files()
    assert not any(f.startswith("Memories/") for f in _files())
    again = vault.sync(OWNER)
    assert again["exported"] == 0 and again["suppressed"] == 0


def test_renaming_an_entity_file_renames_the_entity_instead_of_hiding_it(brain, ent):
    e = ent.upsert_entity(OWNER, "Ada", type="person")
    vault.sync(OWNER)
    old = "Entities/Person/Ada.md"
    os.rename(os.path.join(_root(), old), os.path.join(_root(), "Entities/Person/Ada Lovelace.md"))
    report = vault.sync(OWNER)
    assert report["suppressed"] == 0, report
    got = ent.get_entity(e["id"])
    assert got["hidden"] is False
    assert got["name"] == "Ada Lovelace"
    assert "Entities/Person/Ada Lovelace.md" in _files()
    assert old not in _files()


# ── finding 4: a damaged generated marker / a BOM ────────────────────────

def test_missing_marker_is_refused_and_the_file_is_left_alone(brain):
    engine.add_item("Ada prefiere el te verde", owner=OWNER, trust_class="human_explicit")
    vault.sync(OWNER)
    path = _only("Memories/")
    damaged = _rd(path).replace(render.GENERATED_MARKER + "\n", "")
    _wr(path, damaged)
    report = vault.sync(OWNER)
    assert any("marker" in e for e in report["errors"])
    [m] = _active()
    assert m["text"] == "Ada prefiere el te verde"
    assert _rd(path) == damaged  # not overwritten, not "repaired" by guessing


def test_bom_saved_edit_is_applied(brain):
    engine.add_item("Ada prefiere el te verde", owner=OWNER, trust_class="human_explicit")
    vault.sync(OWNER)
    path = _only("Memories/")
    _wr(path, _rd(path).replace("Ada prefiere el te verde\n", "Ada prefiere el te rojo\n"), encoding="utf-8-sig")
    report = vault.sync(OWNER)
    assert report["errors"] == []
    assert [m["text"] for m in _active()] == ["Ada prefiere el te rojo"]
    new_path = _only("Memories/")
    head = _rd(new_path)
    assert head.startswith("---\nid: ")
    assert "---" not in render.user_zone_of(fm.split(head)[1])


# ── finding 5: the store is the truth for what gets exported ─────────────

def test_edit_saved_between_import_and_export_is_not_lost(brain, monkeypatch):
    a = engine.add_item("Ada prefiere el te verde", owner=OWNER, trust_class="human_explicit")
    vault.sync(OWNER)
    path = _only("Memories/")
    real_plan = vault._build_plan

    def hooked(*args, **kw):
        _wr(path, _rd(path).replace("Ada prefiere el te verde\n", "Ada prefiere el te rojo\n"))
        return real_plan(*args, **kw)

    monkeypatch.setattr(vault, "_build_plan", hooked)
    vault.sync(OWNER)
    monkeypatch.setattr(vault, "_build_plan", real_plan)
    vault.sync(OWNER)
    vault.sync(OWNER)
    assert [m["text"] for m in _active()] == ["Ada prefiere el te rojo"]
    zone = render.user_zone_of(fm.split(_rd(_only("Memories/")))[1])
    assert zone == "Ada prefiere el te rojo"
    assert engine.get_item(a["id"]) is None


def test_store_side_summary_refresh_reaches_the_entity_note(brain, ent):
    e = ent.upsert_entity(OWNER, "Ada", type="person")
    ent.update_entity(e["id"], summary="Ada es ingeniera [mem:aaaa1111].")
    vault.sync(OWNER)
    ent.update_entity(e["id"], summary="Ada es ingeniera en Bluehaven [mem:bbbb2222].")
    vault.sync(OWNER)
    path = "Entities/Person/Ada.md"
    assert render.user_zone_of(fm.split(_rd(path))[1]) == "Ada es ingeniera en Bluehaven [mem:bbbb2222]."
    _wr(path, _rd(path).replace("aliases: []", "aliases:\n- Ada L."))
    vault.sync(OWNER)
    got = ent.get_entity(e["id"])
    assert got["summary"] == "Ada es ingeniera en Bluehaven [mem:bbbb2222]."
    assert got["summary_locked"] is False
    assert got["aliases"] == ["Ada L."]


# ── finding 6: gone sources leave the vault ──────────────────────────────

def test_forgotten_corrected_and_suppressed_memories_leave_disk_and_index(brain):
    a = engine.add_item("Ada vive en Bilbao con su gato", owner=OWNER, trust_class="human_explicit")
    b = engine.add_item("Bruno prefiere reuniones por la manana", owner=OWNER, trust_class="human_explicit")
    c = engine.add_item("Cordera Labs usa la base Orca 15", owner=OWNER, trust_class="human_explicit")
    vault.sync(OWNER)
    engine.correct(a["id"], "Ada vive en Madrid con su gato", reason="chat")
    engine.forget(b["id"], reason="asked")
    engine.set_suppressed(c["id"], True)
    report = vault.sync(OWNER)
    assert report["errors"] == []
    mem_files = [f for f in _files() if f.startswith("Memories/")]
    assert len(mem_files) == 1 and "Madrid" in mem_files[0]
    assert notes.search(OWNER, "Bilbao") == []
    assert notes.search(OWNER, "Orca") == []
    effects = {t["effect"] for t in notes.list_trash(OWNER)}
    assert effects == {"source_gone"}


def test_deleted_objective_and_concept_notes_are_retired(brain, monkeypatch):
    # §176: Objectives/<Project>/ and Concepts/<Project>/ notes had no
    # `_retire_gone_sources` prefix at all, so once their objective/concept
    # (or the whole project) was deleted at the source, the generated note
    # stayed on disk forever.
    project = {"id": "p1", "name": "Bluehaven", "created_at": "2026-01-01"}
    objective = {"id": "o1", "title": "Lanzar beta", "status": "open"}
    concept = {"id": "c1", "name": "Arquitectura hexagonal"}
    state = {"objectives": [objective], "concepts": [concept]}

    monkeypatch.setattr(vault, "_projects_for_owner", lambda owner: [project])
    monkeypatch.setattr(vault, "_objectives_for_project", lambda p: (list(state["objectives"]), True))
    monkeypatch.setattr(vault, "_concepts_for_project", lambda p: (list(state["concepts"]), True))

    vault.sync(OWNER)
    assert any(f.startswith("Projects/") and "Bluehaven" in f for f in _files())
    assert any(f.startswith("Objectives/") and "Lanzar beta" in f for f in _files())
    assert any(f.startswith("Concepts/") and "hexagonal" in f for f in _files())

    # The objective and concept are deleted at the source; the project stays.
    state["objectives"] = []
    state["concepts"] = []
    report = vault.sync(OWNER)
    assert report["errors"] == []
    assert not any(f.startswith("Objectives/") for f in _files())
    assert not any(f.startswith("Concepts/") for f in _files())
    assert any(f.startswith("Projects/") and "Bluehaven" in f for f in _files())
    effects = {t["effect"] for t in notes.list_trash(OWNER)}
    assert effects == {"source_gone"}

    # Now the project itself is deleted: its note is retired too.
    monkeypatch.setattr(vault, "_projects_for_owner", lambda owner: [])
    vault.sync(OWNER)
    assert not any(f.startswith("Projects/") for f in _files())


def test_edited_note_of_a_gone_memory_is_kept_and_reported(brain):
    b = engine.add_item("Bruno prefiere reuniones por la manana", owner=OWNER, trust_class="human_explicit")
    vault.sync(OWNER)
    path = _only("Memories/")
    engine.forget(b["id"], reason="asked")
    _wr(path, _rd(path).replace("por la manana\n", "por la manana y cortas\n"))
    report = vault.sync(OWNER)
    assert any(path in e for e in report["errors"])
    assert path in _files()


# ── finding 7: frontmatter is a patch against the exported base ──────────

def test_frontmatter_edit_does_not_revert_concurrent_store_changes(brain):
    a = engine.add_item("Bruno usa tabuladores para todo", owner=OWNER, trust_class="human_explicit")
    vault.sync(OWNER)
    path = _only("Memories/")
    it = engine.get_item(a["id"])
    it["status"] = "deprecated"
    engine.save_item(it)
    engine.set_pinned(a["id"], True)
    _wr(path, _rd(path).replace("type: fact", "type: preference"))
    vault.sync(OWNER)
    it = engine.get_item(a["id"])
    assert (it["type"], it["status"], it["pinned"]) == ("preference", "deprecated", True)


def test_frontmatter_values_are_validated_and_normalised(brain):
    a = engine.add_item("Ada prefiere el te verde", owner=OWNER, trust_class="human_explicit")
    before = engine.get_item(a["id"])["confidence"]
    vault.sync(OWNER)
    path = _only("Memories/")
    text = _rd(path).replace("level: semantic", "level: Semantic")
    text = text.replace(f"confidence: {before}", "confidence: alta")
    _wr(path, text)
    report = vault.sync(OWNER)
    it = engine.get_item(a["id"])
    assert it["level"] == "semantic"
    assert it["confidence"] == before
    assert any("confidence" in e for e in report["errors"])
    assert a["id"] in engine.pack_detail(owner=OWNER, query="te verde")["ids"]


# ── finding 11: entity names and colliding file names ────────────────────

def test_summary_edit_does_not_mangle_entity_names(brain, ent):
    names = ["Cordera#", "Villanueva S.L.", "Bluehaven.io", "A/B testing"]
    ids = [ent.upsert_entity(OWNER, n, type="tool")["id"] for n in names]
    vault.sync(OWNER)
    for f in _files():
        if f.startswith("Entities/"):
            _wr(f, _rd(f).replace("\n" + render.GENERATED_MARKER, "Una nota mia.\n\n" + render.GENERATED_MARKER, 1))
    report = vault.sync(OWNER)
    assert report["errors"] == []
    assert [ent.get_entity(i)["name"] for i in ids] == names
    assert all(ent.get_entity(i)["summary"] == "Una nota mia." for i in ids)


def test_entities_whose_file_names_collide_both_get_a_stable_note(brain, ent):
    a = ent.upsert_entity(OWNER, "Orca#", type="tool")
    b = ent.upsert_entity(OWNER, "Orca", type="tool")
    ent.update_entity(a["id"], summary="Lenguaje de Cordera Labs")
    ent.update_entity(b["id"], summary="Lenguaje de sistemas")
    vault.sync(OWNER)
    vault.sync(OWNER)
    third = vault.sync(OWNER)
    assert third["exported"] == 0
    tool_notes = [f for f in _files() if f.startswith("Entities/Tool/")]
    assert len(tool_notes) == 2
    sources = {fm.split(_rd(f))[0]["source"] for f in tool_notes}
    assert sources == {f"ent:{a['id']}", f"ent:{b['id']}"}


# ── finding 12: frontmatter round trip ───────────────────────────────────

def test_dates_and_plain_words_stay_strings():
    data, _ = fm.split("---\nvalid_until: 2030-01-01T00:00:00Z\ncreated: 2025-01-01\n"
                       "time: 10:30\nver: 1_000\nnote: yes\n---\nbody\n")
    assert data == {"valid_until": "2030-01-01T00:00:00Z", "created": "2025-01-01",
                    "time": "10:30", "ver": "1_000", "note": "yes"}


def test_a_leading_horizontal_rule_block_is_body_not_frontmatter():
    text = "---\nPrimera seccion de la reunion, sin dos puntos\n---\nSegunda seccion.\n"
    data, body = fm.split(text)
    assert data == {} and body == text


def test_unquoted_dates_saved_by_an_editor_are_a_noop(brain):
    import re
    a = engine.add_item("Ada prefiere el te verde", owner=OWNER, trust_class="human_explicit",
                        valid_until="2030-01-01T00:00:00Z")
    vault.sync(OWNER)
    path = _only("Memories/")
    text = re.sub(r"'(\d{4}-\d\d-\d\dT[\d:]+Z)'", r"\1", _rd(path))
    res = notes.write_note(OWNER, path, text)
    assert res["applied"]["errors"] == []
    it = engine.get_item(a["id"])
    assert it["valid_until"] == "2030-01-01T00:00:00Z"
    assert fm.split(_rd(res["note"]["path"]))[0]["valid_until"] == "2030-01-01T00:00:00Z"


def test_renaming_a_note_keeps_other_notes_content_and_yaml_verbatim(brain):
    _wr("Notes/Ada.md", "Nota sobre Ada.\n")
    diario = "---\nPrimera seccion de la reunion, sin dos puntos\n---\nSegunda seccion con [[Ada]].\n"
    _wr("Notes/Diario.md", diario)
    custom = "---\n# my header comment\ntags: [a, b]\ntime: 10:30\n---\nVer [[Ada]].\n"
    _wr("Notes/Otro.md", custom)
    notes.reindex(OWNER)
    out = notes.rename_note(OWNER, "Notes/Ada.md", "Ada Lovelace")
    assert out["updated_links"] == 2
    assert _rd("Notes/Diario.md") == diario.replace("[[Ada]]", "[[Ada Lovelace]]")
    assert _rd("Notes/Otro.md") == custom.replace("[[Ada]]", "[[Ada Lovelace]]")


# ── finding 13: generated links resolve ──────────────────────────────────

def test_memory_links_resolve_even_with_unsafe_characters(brain):
    engine.add_item("Bruno prefiere que los despliegues a produccion se hagan siempre los martes por la manana",
                    owner=OWNER, trust_class="human_explicit")
    engine.add_item("Regla: nunca usar #hashtags en commits", owner=OWNER, trust_class="human_explicit")
    vault.sync(OWNER)
    assert notes.unresolved(OWNER) == []


def test_workspace_path_memories_land_in_their_project_and_basenames_are_unique(brain, ent, monkeypatch):
    workspace = os.path.join(str(brain), "work", "bluehaven")
    monkeypatch.setattr(vault, "_projects_for_owner",
                        lambda owner: [{"id": "3f2a9c1e", "name": "Bluehaven", "workspace": workspace}])
    monkeypatch.setattr(vault, "_objectives_for_project", lambda p: ([], True))
    monkeypatch.setattr(vault, "_concepts_for_project", lambda p: ([], True))
    engine.add_item("El backend usa Lince", owner=OWNER, project=workspace, trust_class="human_explicit")
    engine.add_item("El frontend usa Orca", owner=OWNER, project="3f2a9c1e", trust_class="human_explicit")
    ent.upsert_entity(OWNER, "Bluehaven", type="project", project="3f2a9c1e")
    vault.sync(OWNER)
    assert notes.unresolved(OWNER) == []
    stems = [os.path.splitext(os.path.basename(f))[0].casefold() for f in _files()]
    assert len(stems) == len(set(stems))
    project_note = _rd("Projects/Bluehaven.md")
    assert "Lince" in project_note and "Orca" in project_note
    mem = notes.read_note(OWNER, _only("Memories/", "Lince"))
    assert [link["path"] for link in mem["links"] if link["target"] == "Bluehaven"] == ["Projects/Bluehaven.md"]


# ── finding 15: one bad file never breaks the index ─────────────────────

def test_latin1_file_and_scalar_tags_do_not_break_reindex(brain):
    engine.add_item("Ada prefiere el te verde", owner=OWNER, trust_class="human_explicit")
    vault.sync(OWNER)
    os.makedirs(os.path.join(_root(), "Notes"), exist_ok=True)
    with open(os.path.join(_root(), "Notes", "viejo.md"), "wb") as fh:
        fh.write("Reuni\xf3n con Bruno".encode("latin-1"))
    _wr("Notes/t.md", "---\ntags: 2024\n---\nbody\n")
    report = vault.sync(OWNER)
    assert not any("reindex" in e for e in report["errors"])
    assert notes.search(OWNER, "verde")
    notes.write_note(OWNER, "Notes/nueva.md", "hola [[Ada]]")
    assert {"tag": "2024", "count": 1} in notes.tags(OWNER)
    assert wikilinks.tags("", {"tags": "idea"}) == ["idea"]


# ── finding 16: a long structured note is not squashed into a memory ─────

def test_long_structured_note_under_memories_is_left_as_a_free_note(brain):
    vault.sync(OWNER)
    body = "---\ntags:\n- ideas\n---\n# Plan de Bluehaven\n\n" + "\n".join(
        f"- Paso {i}: revisar el modulo {i} con Bruno y documentar el resultado" for i in range(60)) + "\n"
    _wr("Memories/Ideas/plan.md", body)
    report = vault.sync(OWNER)
    assert report["created"] == 0
    assert any("plan.md" in e for e in report["errors"])
    assert _rd("Memories/Ideas/plan.md") == body
    assert engine.list_items(owner=OWNER) == []
    again = vault.sync(OWNER)
    assert again["errors"] == []  # reported once, not on every sync


def test_short_note_under_memories_is_imported_and_the_original_trashed(brain):
    _wr("Memories/Facts/nuevo.md", "Cordera Labs usa la base Orca 16.\n")
    report = vault.sync(OWNER)
    assert report["created"] == 1
    assert "Memories/Facts/nuevo.md" not in _files()
    trash = notes.list_trash(OWNER)
    assert [t["path"] for t in trash] == ["Memories/Facts/nuevo.md"]


# ── performance: a no-op sync reads nothing it already knows ─────────────

def test_noop_sync_does_not_reread_or_rewrite_unchanged_files(brain, monkeypatch):
    for i in range(30):
        engine.add_item(f"Hecho numero {i} sobre Bluehaven", owner=OWNER, trust_class="human_explicit")
    vault.sync(OWNER)
    reads = []
    real_read = vault._read_checked
    monkeypatch.setattr(vault, "_read_checked", lambda p: (reads.append(p), real_read(p))[1])
    real_split = fm.split
    splits = []
    monkeypatch.setattr(fm, "split", lambda t: (splits.append(1), real_split(t))[1])
    report = vault.sync(OWNER)
    assert report["exported"] == 0 and report["imported"] == 0
    assert reads == []
    assert splits == []


# ── multi-owner ──────────────────────────────────────────────────────────

def test_shared_vault_does_not_export_other_owners_personal_memories(brain):
    mgr = memmod.MemoryManager(constants.DATA_DIR)
    e1 = mgr.add_entry("El codigo del trastero de Bruno es el de siempre", "chat", "fact", "bruno")
    e2 = mgr.add_entry("Ada tiene cita el martes", "chat", "fact", "ada")
    e3 = mgr.add_entry("Nota compartida sin dueno", "chat", "fact", None)
    mgr.save([e1, e2, e3])
    vault.sync("")
    personal = [f for f in _files("") if f.startswith("Personal/")]
    assert len(personal) == 1 and "compartida" in personal[0]


def test_a_foreign_source_id_is_never_touched_through_this_vault(brain, ent):
    other = engine.add_item("Bruno guarda su clave en un cajon", owner="bruno", trust_class="human_explicit")
    other_ent = ent.upsert_entity("bruno", "Villanueva", type="organization")
    mgr = memmod.MemoryManager(constants.DATA_DIR)
    pe = mgr.add_entry("Dato personal de Bruno", "chat", "fact", "bruno")
    mgr.save([pe])
    vault.sync(OWNER)
    marker = render.GENERATED_MARKER
    _wr("Memories/Facts/x.md", f"---\nid: {other['id']}\nsource: mem:{other['id']}\nkind: memory\n---\n\nTexto cambiado\n\n{marker}\n")
    _wr("Personal/y.md", f"---\nid: {pe['id']}\nsource: pmem:{pe['id']}\nkind: personal\n---\n\nOtro texto\n\n{marker}\n")
    _wr("Entities/Organization/z.md", f"---\nid: {other_ent['id']}\nsource: ent:{other_ent['id']}\nkind: entity\n---\n\nResumen\n\n{marker}\n")
    report = vault.sync(OWNER)
    assert len([e for e in report["errors"] if "another owner" in e]) == 3
    assert engine.get_item(other["id"])["text"] == "Bruno guarda su clave en un cajon"
    assert mgr.load("bruno")[0]["text"] == "Dato personal de Bruno"
    assert ent.get_entity(other_ent["id"])["summary"] == ""
    notes.delete_note(OWNER, "Memories/Facts/x.md")
    notes.delete_note(OWNER, "Entities/Organization/z.md")
    notes.delete_note(OWNER, "Personal/y.md")
    assert engine.get_item(other["id"])["suppressed"] is False
    assert ent.get_entity(other_ent["id"])["hidden"] is False
    assert len(mgr.load("bruno")) == 1


# ── minor: search with punctuation inside a word ─────────────────────────

def test_search_tolerates_quotes_and_operators(brain):
    _wr("Notes/a.md", 'Reunion con Ada sobre "hola mundo" y C++ NEAR la costa (urgente) - pendiente*')
    notes.reindex(OWNER)
    for q in ['"hola', 'hola"', 'NEAR(', 'urgente)', "O'Brien ada", 'NEAR ada', 'ada:', '^ada', 'c++']:
        hits = [r["path"] for r in notes.search(OWNER, q)]
        if q != "O'Brien ada":
            assert hits == ["Notes/a.md"], q
    assert notes.search(OWNER, "*") == []


def test_patch_header_rewrites_only_the_given_keys():
    text = "---\n# kept comment\ntitle: x\nupdated: '2020-01-01'\ntime: 10:30\ntags:\n- a\n---\nbody\n"
    header = fm.patch_header(text, {"updated": "2026-09-23T00:00:00Z", "new_key": "v"})
    assert header == ("---\n# kept comment\ntitle: x\nupdated: '2026-09-23T00:00:00Z'\ntime: 10:30\n"
                      "tags:\n- a\nnew_key: v\n---\n")
    assert fm.patch_header("plain body", {"updated": "u"}) == "---\nupdated: u\n---\n"


def test_agent_append_keeps_a_free_notes_yaml_verbatim(brain):
    from src.agent_tools import brain_tools
    custom = "---\n# my header comment\ntags: [a, b]\ntime: 10:30\n---\n\nFirst line.\n"
    _wr("Notes/Libre.md", custom)
    notes.reindex(OWNER)
    composed = brain_tools._compose_edit(notes, render, fm, bdb, OWNER, "Notes/Libre.md", "First line.\n\nSecond.")
    assert composed.startswith("---\n# my header comment\ntags: [a, b]\ntime: 10:30\nupdated: ")
    assert composed.endswith("\nFirst line.\n\nSecond.\n")


def test_entity_note_follows_a_change_in_a_mentioning_memory(brain, ent):
    e = ent.upsert_entity(OWNER, "Ada", type="person")
    item = engine.add_item("Ada trabaja en Cordera Labs", owner=OWNER, trust_class="human_explicit")
    ent.add_mention(OWNER, e["id"], f"mem:{item['id']}")
    vault.sync(OWNER)
    assert "Ada trabaja en Cordera Labs" in _rd("Entities/Person/Ada.md")
    stored = engine.get_item(item["id"])
    stored["text"] = "Ada trabaja en Bluehaven"
    engine.save_item(stored)
    vault.sync(OWNER)
    assert "Ada trabaja en Bluehaven" in _rd("Entities/Person/Ada.md")
    assert vault.sync(OWNER)["exported"] == 0
