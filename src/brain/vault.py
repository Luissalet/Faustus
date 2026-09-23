"""
brain/vault.py — the markdown vault on disk and its two-way sync with the
stores it mirrors (`memory_engine`, `memory.json`, entities from Lot B).

Why sync instead of a live view: the files are meant to be opened in any
editor and hand-edited — that is the whole point of a vault — so folding a
human's edit back into the store it mirrors has to be a deliberate,
auditable step, not a filesystem watcher racing someone's keystrokes.
`sync()` runs on demand (the UI's "Sync now") or on a timer
(`brain_vault_sync_seconds`), always import-then-export-then-reindex, so a
human edit always wins over whatever the store looked like a moment before
(MEM-02's tombstone discipline in `memory_engine` protects the rest).

Only three kinds of note round-trip into a store: `mem:` (memory_engine),
`pmem:` (memory.json) and `ent:` (Lot B's entities — imported lazily, and
every entity feature degrades to "not shown" when Lot B is not installed).
Everything else the vault writes (Project/Objective/Concept/Daily/Home) is a
generated index: re-rendered every sync, its free-text "user zone" always
preserved. Free notes under Notes/ round-trip nowhere — the file itself is
the only copy, and sync just keeps it indexed.
"""

from __future__ import annotations

import contextlib
import logging
import os
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from . import db, frontmatter as fm, render

logger = logging.getLogger(__name__)

SCHEMA_NAME = "brain_vault"

db.register_schema(SCHEMA_NAME, [
    """CREATE TABLE IF NOT EXISTS notes (
        owner TEXT NOT NULL, path TEXT NOT NULL, title TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '',
        hash TEXT NOT NULL DEFAULT '', mtime REAL NOT NULL DEFAULT 0,
        size INTEGER NOT NULL DEFAULT 0, frontmatter TEXT NOT NULL DEFAULT '{}',
        tags TEXT NOT NULL DEFAULT '[]', updated_at TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (owner, path)
    )""",
    """CREATE TABLE IF NOT EXISTS links (
        owner TEXT NOT NULL, src_path TEXT NOT NULL, target TEXT NOT NULL,
        dst_path TEXT, label TEXT NOT NULL DEFAULT '',
        is_embed INTEGER NOT NULL DEFAULT 0
    )""",
    "CREATE INDEX IF NOT EXISTS brain_links_src ON links(owner, src_path)",
    "CREATE INDEX IF NOT EXISTS brain_links_dst ON links(owner, dst_path)",
    "CREATE INDEX IF NOT EXISTS brain_links_target ON links(owner, target)",
    """CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
        owner UNINDEXED, path UNINDEXED, title, body
    )""",
    """CREATE TABLE IF NOT EXISTS sync_state (
        owner TEXT NOT NULL, path TEXT NOT NULL, source TEXT NOT NULL DEFAULT '',
        rendered_hash TEXT NOT NULL DEFAULT '', file_hash TEXT NOT NULL DEFAULT '',
        synced_at TEXT NOT NULL DEFAULT '', PRIMARY KEY (owner, path)
    )""",
    """CREATE TABLE IF NOT EXISTS user_zones (
        owner TEXT NOT NULL, path TEXT NOT NULL, text TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL DEFAULT '', PRIMARY KEY (owner, path)
    )""",
    """CREATE TABLE IF NOT EXISTS trash (
        id TEXT PRIMARY KEY, owner TEXT NOT NULL, path TEXT NOT NULL,
        title TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '',
        content TEXT NOT NULL DEFAULT '', effect TEXT NOT NULL DEFAULT '',
        deleted_at TEXT NOT NULL DEFAULT '', restored_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS sync_runs (
        id TEXT PRIMARY KEY, owner TEXT NOT NULL, at TEXT NOT NULL,
        report TEXT NOT NULL DEFAULT '{}'
    )""",
])

MIRRORED_PREFIXES = ("mem:", "pmem:", "ent:")


class VaultPathError(ValueError):
    """A note path this vault refuses: traversal, absolute, non-.md, .trash/."""


# ── paths ────────────────────────────────────────────────────────────────

def vault_root(owner: str = "") -> str:
    from src.settings import get_setting
    base = str(get_setting("brain_vault_dir") or "").strip() or os.path.join(db.brain_dir(), "vault")
    return os.path.join(base, db.owner_key(owner))


def validate_path(path: Any) -> str:
    """Normalize a vault-relative note path, or raise `VaultPathError`."""
    text = str(path or "").strip().replace("\\", "/")
    if not text:
        raise VaultPathError("empty path")
    if text.startswith("/") or (len(text) > 1 and text[1] == ":"):
        raise VaultPathError(f"absolute paths are not allowed: {path!r}")
    parts = [p for p in text.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        raise VaultPathError(f"path traversal is not allowed: {path!r}")
    if parts[0] == ".trash":
        raise VaultPathError(".trash is internal")
    if not parts[-1].lower().endswith(".md"):
        raise VaultPathError("only .md notes are allowed")
    return "/".join(parts)


def abs_path(owner: str, rel_path: str) -> str:
    rel = validate_path(rel_path)
    return os.path.join(vault_root(owner), *rel.split("/"))


def _iter_files(root: str):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".trash"]
        for name in filenames:
            if name.lower().endswith(".md"):
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, root).replace(os.sep, "/")
                yield rel, full


def _read(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except (FileNotFoundError, IsADirectoryError):
        return None


def _atomic_write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp{uuid.uuid4().hex[:8]}"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _title_from_path(rel_path: str) -> str:
    return os.path.splitext(os.path.basename(rel_path))[0]


# ── entities: optional (Lot B), imported lazily every time it's needed ──

def _entities():
    # importlib goes through sys.modules first, so a substituted module (a
    # test double, or a reload) is honoured even after the package attribute
    # was bound by an earlier import.
    try:
        import importlib
        return importlib.import_module("src.brain.entities")
    except ImportError:
        return None


def _entity_links_for(source_ref: str) -> List[Dict[str, Any]]:
    ent = _entities()
    if ent is None:
        return []
    try:
        return list(ent.mentions_for(source_ref) or [])
    except Exception:  # noqa: BLE001 - never block a render on Lot B trouble
        logger.debug("brain vault: mentions_for failed for %s", source_ref, exc_info=True)
        return []


def _personal_data_dir() -> str:
    from src import constants
    return constants.DATA_DIR


# ── small DB helpers ─────────────────────────────────────────────────────

def _all_sync_state(owner: str) -> Dict[str, Dict[str, Any]]:
    with db.db() as conn:
        rows = conn.execute("SELECT * FROM sync_state WHERE owner=?", (owner,)).fetchall()
    return {row["path"]: dict(row) for row in rows}


def _count_sync_state(owner: str) -> int:
    with db.db() as conn:
        row = conn.execute("SELECT COUNT(*) c FROM sync_state WHERE owner=?", (owner,)).fetchone()
    return int(row["c"]) if row else 0


def _known_paths_by_source(owner: str) -> Dict[str, str]:
    with db.db() as conn:
        rows = conn.execute(
            "SELECT source, path FROM notes WHERE owner=? AND source != ''", (owner,)
        ).fetchall()
    return {row["source"]: row["path"] for row in rows}


def _db_delete_sync_state(owner: str, path: str) -> None:
    with db.db() as conn:
        conn.execute("DELETE FROM sync_state WHERE owner=? AND path=?", (owner, path))


def _db_delete_notes_row(owner: str, path: str) -> None:
    with db.db() as conn:
        conn.execute("DELETE FROM notes WHERE owner=? AND path=?", (owner, path))
        conn.execute("DELETE FROM links WHERE owner=? AND src_path=?", (owner, path))
        conn.execute("DELETE FROM notes_fts WHERE owner=? AND path=?", (owner, path))
        conn.execute("DELETE FROM user_zones WHERE owner=? AND path=?", (owner, path))


def _get_user_zone_cache(owner: str, path: str) -> Optional[str]:
    with db.db() as conn:
        row = conn.execute(
            "SELECT text FROM user_zones WHERE owner=? AND path=?", (owner, path)
        ).fetchone()
    return row["text"] if row else None


def _trash_add(owner: str, path: str, title: str, source: str, content: str, effect: str) -> str:
    trash_id = uuid.uuid4().hex
    with db.db() as conn:
        conn.execute(
            "INSERT INTO trash (id, owner, path, title, source, content, effect, deleted_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (trash_id, owner, path, title, source, content, effect, db.now_iso()),
        )
    return trash_id


def _record_run(owner: str, report: Dict[str, Any]) -> None:
    with db.db() as conn:
        conn.execute(
            "INSERT INTO sync_runs (id, owner, at, report) VALUES (?,?,?,?)",
            (uuid.uuid4().hex, owner, report["at"], db.dumps(report)),
        )


def last_sync(owner: str = "") -> Optional[Dict[str, Any]]:
    owner = str(owner or "")
    with db.db() as conn:
        row = conn.execute(
            "SELECT report FROM sync_runs WHERE owner=? ORDER BY at DESC, rowid DESC LIMIT 1",
            (owner,),
        ).fetchone()
    return db.loads(row["report"], None) if row else None


# ── export: one item's source/path/render-function ──────────────────────

def _memory_target(item: Dict[str, Any]):
    source = f"mem:{item['id']}"
    folder = render.TYPE_FOLDERS.get(item.get("type"), "Facts")
    filename = db.safe_filename(render.memory_title_text(item)) + f" ({str(item['id'])[:8]})"
    rel_path = f"Memories/{folder}/{filename}.md"
    entity_links = _entity_links_for(source)

    def render_fn(uz: str, item=item, entity_links=entity_links) -> str:
        return render.render_memory_note(item, entities=entity_links, user_zone=uz)

    return source, rel_path, render_fn


def _personal_target(entry: Dict[str, Any]):
    source = f"pmem:{entry.get('id')}"
    title = " ".join(str(entry.get("text") or "").split())[:60] or "nota"
    filename = db.safe_filename(title) + f" ({str(entry.get('id'))[:8]})"
    rel_path = f"Personal/{filename}.md"

    def render_fn(uz: str, entry=entry) -> str:
        return render.render_personal_note(entry, user_zone=uz)

    return source, rel_path, render_fn


def _entity_target(entity: Dict[str, Any], profile: Dict[str, Any]):
    source = f"ent:{entity['id']}"
    folder = str(entity.get("type") or "other").capitalize()
    rel_path = f"Entities/{folder}/{db.safe_filename(entity.get('name', ''))}.md"

    def render_fn(uz: str, entity=entity, profile=profile) -> str:
        return render.render_entity_note(entity, profile, user_zone=uz)

    return source, rel_path, render_fn


def _read_user_zone(owner: str, rel_path: str) -> str:
    existing = _read(abs_path(owner, rel_path))
    if existing is not None:
        _, body = fm.split(existing)
        return render.user_zone_of(body)
    cached = _get_user_zone_cache(owner, rel_path)
    return cached if cached is not None else ""


def _write_if_changed(owner: str, rel_path: str, source: str, text: str) -> bool:
    full = abs_path(owner, rel_path)
    existing = _read(full)
    changed = existing != text
    if changed:
        _atomic_write(full, text)
    file_hash = db.sha(text)
    generated_idx = text.find(render.GENERATED_MARKER)
    rendered_hash = db.sha(text[generated_idx:]) if generated_idx >= 0 else ""
    now = db.now_iso()
    if source.startswith(MIRRORED_PREFIXES):
        with db.db() as conn:
            conn.execute(
                "INSERT INTO sync_state (owner, path, source, rendered_hash, file_hash, synced_at) "
                "VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(owner, path) DO UPDATE SET source=excluded.source, "
                "rendered_hash=excluded.rendered_hash, file_hash=excluded.file_hash, "
                "synced_at=excluded.synced_at",
                (owner, rel_path, source, rendered_hash, file_hash, now),
            )
    _, body = fm.split(text)
    user_zone = render.user_zone_of(body)
    with db.db() as conn:
        conn.execute(
            "INSERT INTO user_zones (owner, path, text, updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(owner, path) DO UPDATE SET text=excluded.text, updated_at=excluded.updated_at",
            (owner, rel_path, user_zone, now),
        )
    return changed


def _export_one(owner: str, source: str, rel_path: str, render_fn, known_by_source: Dict[str, str],
                report: Dict[str, Any]) -> str:
    old_path = known_by_source.pop(source, None)
    if old_path and old_path != rel_path:
        with contextlib.suppress(OSError):
            os.remove(abs_path(owner, old_path))
        _db_delete_sync_state(owner, old_path)
        _db_delete_notes_row(owner, old_path)
    user_zone = _read_user_zone(owner, rel_path)
    text = render_fn(user_zone)
    if _write_if_changed(owner, rel_path, source, text):
        report["exported"] += 1
    return rel_path


def _export_single(owner: str, source: str) -> Optional[str]:
    """Re-render and (if needed) write just one already-known source right
    now — used right after a single-file import so the UI sees the result
    immediately, without waiting for the next full sync."""
    known_by_source = _known_paths_by_source(owner)
    report = {"exported": 0}
    if source.startswith("mem:"):
        from src import memory_engine as engine
        item = engine.get_item(source[4:])
        if not item or item.get("suppressed") or item.get("sensitivity") == "secret":
            return None
        s, rel_path, render_fn = _memory_target(item)
    elif source.startswith("pmem:"):
        from src import memory as memmod
        mgr = memmod.MemoryManager(_personal_data_dir())
        entry = next((e for e in mgr.load(None) if str(e.get("id")) == source[5:]), None)
        if not entry:
            return None
        s, rel_path, render_fn = _personal_target(entry)
    elif source.startswith("ent:"):
        ent = _entities()
        if ent is None:
            return None
        entity = ent.get_entity(source[4:])
        if not entity or entity.get("hidden"):
            return None
        try:
            profile = ent.profile(entity["id"])
        except Exception:  # noqa: BLE001
            profile = {}
        s, rel_path, render_fn = _entity_target(entity, profile)
    else:
        return None
    return _export_one(owner, s, rel_path, render_fn, known_by_source, report)


# ── export: whole categories (the periodic sync) ────────────────────────

def _active_memories(owner: str, **filters) -> List[Dict[str, Any]]:
    from src import memory_engine as engine
    items = engine.list_items(owner=owner, limit=2000, **filters)
    return [
        m for m in items
        if not m.get("suppressed") and m.get("sensitivity") != "secret"
        and m.get("status") in ("active", "deprecated", "anti_pattern")
    ]


def _export_memories(owner: str, known_by_source: Dict[str, str], report: Dict[str, Any],
                     skip_sources: "set[str]" = frozenset()) -> None:
    for item in _active_memories(owner):
        source, rel_path, render_fn = _memory_target(item)
        if source in skip_sources:
            continue
        _export_one(owner, source, rel_path, render_fn, known_by_source, report)


def _export_personal(owner: str, known_by_source: Dict[str, str], report: Dict[str, Any],
                     skip_sources: "set[str]" = frozenset()) -> None:
    from src import memory as memmod
    mgr = memmod.MemoryManager(_personal_data_dir())
    for entry in mgr.load(owner if owner else None):
        source, rel_path, render_fn = _personal_target(entry)
        if source in skip_sources:
            continue
        _export_one(owner, source, rel_path, render_fn, known_by_source, report)


def _export_entities(owner: str, known_by_source: Dict[str, str], report: Dict[str, Any],
                     skip_sources: "set[str]" = frozenset()) -> None:
    ent = _entities()
    if ent is None:
        return
    for entity in ent.list_entities(owner=owner, limit=2000):
        if entity.get("hidden") or f"ent:{entity['id']}" in skip_sources:
            continue
        try:
            profile = ent.profile(entity["id"])
        except Exception:  # noqa: BLE001
            profile = {}
        source, rel_path, render_fn = _entity_target(entity, profile)
        _export_one(owner, source, rel_path, render_fn, known_by_source, report)


# Overridable in tests (each wraps a store this lot does not own) — kept as
# module functions rather than inlined so a test can monkeypatch just the
# data source without touching real projects/objectives/concepts storage.

def _projects_for_owner(owner: str) -> List[Dict[str, Any]]:
    try:
        from services import projects as projects_mod
        return projects_mod.get_store().list(owner=owner)
    except Exception:  # noqa: BLE001
        logger.debug("brain vault: could not list projects", exc_info=True)
        return []


def _objectives_for_project(project: Dict[str, Any]) -> List[Dict[str, Any]]:
    try:
        from services import objectives as objectives_mod
        state = objectives_mod.load_state(project)
        return list((state.get("objectives") or {}).values())
    except Exception:  # noqa: BLE001
        logger.debug("brain vault: could not load objectives", exc_info=True)
        return []


def _concepts_for_project(project: Dict[str, Any]) -> List[Dict[str, Any]]:
    try:
        from src import project_concepts
        key = project_concepts.resolve_project_key(project_id=project.get("id"))
        return project_concepts.Store(key).all_concepts()
    except Exception:  # noqa: BLE001
        logger.debug("brain vault: could not load concepts", exc_info=True)
        return []


def _entities_for_project(owner: str, project_id: str) -> List[Dict[str, Any]]:
    ent = _entities()
    if ent is None:
        return []
    try:
        found = ent.list_entities(owner=owner, limit=500)
        return [e for e in found if (e.get("project") or "") == project_id and not e.get("hidden")]
    except Exception:  # noqa: BLE001
        return []


def _latest_daily_dates(owner: str, limit: int = 10) -> List[str]:
    root = os.path.join(vault_root(owner), "Daily")
    if not os.path.isdir(root):
        return []
    names = [name[:-3] for name in os.listdir(root) if name.lower().endswith(".md")]
    return sorted(names, reverse=True)[:limit]


def _export_projects(owner: str, projects_list: List[Dict[str, Any]],
                     known_by_source: Dict[str, str], report: Dict[str, Any]) -> None:
    for project in projects_list:
        pid = str(project.get("id") or "")
        pname = str(project.get("name") or pid)
        memories = _active_memories(owner, project=pid)
        objectives = [o for o in _objectives_for_project(project) if o.get("status") != "dropped"]
        concepts = _concepts_for_project(project)
        entity_links = _entities_for_project(owner, pid)

        source = f"proj:{pid}"
        rel_path = f"Projects/{db.safe_filename(pname)}.md"

        def project_render(uz, project=project, memories=memories, objectives=objectives,
                           concepts=concepts, entity_links=entity_links) -> str:
            return render.render_project_note(
                project, memories=memories, objectives=objectives,
                concepts=concepts, entities=entity_links, user_zone=uz,
            )

        _export_one(owner, source, rel_path, project_render, known_by_source, report)

        for obj in objectives:
            osource = f"obj:{pname}/{obj.get('id')}"
            orel = (f"Objectives/{db.safe_filename(pname)}/"
                   f"{db.safe_filename(str(obj.get('title') or obj.get('id')))}.md")

            def obj_render(uz, obj=obj, pname=pname) -> str:
                return render.render_objective_note(obj, pname, user_zone=uz)

            _export_one(owner, osource, orel, obj_render, known_by_source, report)

        for concept in concepts:
            csource = f"concept:{pname}/{concept.get('id')}"
            crel = (f"Concepts/{db.safe_filename(pname)}/"
                   f"{db.safe_filename(str(concept.get('name') or concept.get('id')))}.md")

            def concept_render(uz, concept=concept, pname=pname) -> str:
                return render.render_concept_note(concept, pname, user_zone=uz)

            _export_one(owner, csource, crel, concept_render, known_by_source, report)


def _export_home(owner: str, projects_list: List[Dict[str, Any]],
                 known_by_source: Dict[str, str], report: Dict[str, Any]) -> None:
    latest_memories = _active_memories(owner)[:20]
    entity_counts: List[Dict[str, Any]] = []
    ent = _entities()
    if ent is not None:
        try:
            counts: Dict[str, int] = {}
            for entity in ent.list_entities(owner=owner, limit=5000):
                if entity.get("hidden"):
                    continue
                etype = str(entity.get("type") or "other")
                counts[etype] = counts.get(etype, 0) + 1
            entity_counts = [{"type": t, "count": c} for t, c in sorted(counts.items())]
        except Exception:  # noqa: BLE001
            entity_counts = []
    latest_daily = _latest_daily_dates(owner)

    def home_render(uz) -> str:
        return render.render_home(
            projects=projects_list, entity_counts=entity_counts,
            latest_memories=latest_memories, latest_daily=latest_daily, user_zone=uz,
        )

    _export_one(owner, "home", "Home.md", home_render, known_by_source, report)


# ── import: fold a human's edit of a mirrored file back into its store ──

def _process_mem_file(owner: str, rel_path: str, file_text: str, item_id: str,
                      report: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    from src import memory_engine as engine

    baseline = engine.get_item(item_id)
    if not baseline:
        report["errors"].append(f"{rel_path}: unknown memory id {item_id}")
        return None
    new_fields, body = fm.split(file_text)
    new_text = render.user_zone_of(body).strip()
    target = baseline
    if new_text and new_text != str(baseline.get("text") or "").strip():
        corrected = engine.correct(item_id, new_text, reason="edited in vault")
        if corrected:
            target = corrected
            with contextlib.suppress(OSError):
                os.remove(abs_path(owner, rel_path))
            _db_delete_sync_state(owner, rel_path)
            _db_delete_notes_row(owner, rel_path)

    field_map = ("type", "level", "confidence", "valid_from", "valid_until", "status")
    updates = {
        key: new_fields[key] for key in field_map
        if key in new_fields and new_fields[key] != baseline.get(key)
    }
    if updates:
        merged = dict(target)
        merged.update(updates)
        merged["updated_at"] = db.now_iso()
        engine.save_item(merged)
        target = engine.get_item(target["id"]) or target

    if "pinned" in new_fields and bool(new_fields["pinned"]) != bool(baseline.get("pinned")):
        engine.set_pinned(target["id"], bool(new_fields["pinned"]))
        target = engine.get_item(target["id"]) or target

    return target


def _process_pmem_file(owner: str, rel_path: str, file_text: str, entry_id: str,
                       report: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    from src import memory as memmod

    mgr = memmod.MemoryManager(_personal_data_dir())
    try:
        entries = mgr.load_all_for_update()
    except memmod.MemoryStoreUnreadable as exc:
        report["errors"].append(f"{rel_path}: personal store unreadable ({exc})")
        return None
    new_fields, body = fm.split(file_text)
    new_text = render.user_zone_of(body).strip()
    target = None
    changed = False
    for entry in entries:
        if str(entry.get("id")) != entry_id:
            continue
        target = entry
        if new_text and new_text != str(entry.get("text") or "").strip():
            entry["text"] = new_text
            changed = True
        new_category = new_fields.get("type")
        if new_category and new_category != entry.get("category"):
            entry["category"] = new_category
            changed = True
        break
    if target is None:
        report["errors"].append(f"{rel_path}: unknown personal memory id {entry_id}")
        return None
    if changed:
        mgr.save(entries)
    return target


def _process_ent_file(owner: str, rel_path: str, file_text: str, entity_id: str,
                      report: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    ent = _entities()
    if ent is None:
        report["errors"].append(f"{rel_path}: entities module unavailable, edit not applied")
        return None
    entity = ent.get_entity(entity_id)
    if not entity:
        report["errors"].append(f"{rel_path}: unknown entity id {entity_id}")
        return None
    new_fields, body = fm.split(file_text)
    new_summary = render.user_zone_of(body).strip()
    title = _title_from_path(rel_path)
    updates: Dict[str, Any] = {}
    if title and title != entity.get("name"):
        updates["name"] = title
    new_type = new_fields.get("type")
    if new_type and new_type != entity.get("type"):
        updates["type"] = new_type
    new_aliases = new_fields.get("aliases")
    if new_aliases is not None and list(new_aliases) != list(entity.get("aliases") or []):
        updates["aliases"] = list(new_aliases)
    if new_summary and new_summary != str(entity.get("summary") or "").strip():
        updates["summary"] = new_summary
        updates["summary_locked"] = True
    if updates:
        ent.update_entity(entity_id, **updates)
        entity = ent.get_entity(entity_id) or entity
    return entity


def _process_new_memory(owner: str, rel_path: str, file_text: str,
                        report: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    from src import memory_engine as engine

    fm_data, body = fm.split(file_text)
    text = render.user_zone_of(body).strip() or body.strip()
    if not text:
        report["errors"].append(f"{rel_path}: empty note, skipped")
        return None
    parts = rel_path.split("/")
    item_type = "fact"
    if len(parts) >= 2:
        for candidate_type, folder_name in render.TYPE_FOLDERS.items():
            if folder_name == parts[1]:
                item_type = candidate_type
                break
    try:
        item = engine.add_item(text, owner=owner, type=item_type, trust_class="human_explicit")
    except engine.MemoryEngineError as exc:
        report["errors"].append(f"{rel_path}: {exc}")
        return None
    with contextlib.suppress(OSError):
        os.remove(abs_path(owner, rel_path))
    _db_delete_notes_row(owner, rel_path)
    return item


# ── deletions: a mirrored file disappeared ──────────────────────────────

def _apply_deletions(owner: str, missing_rows: List[Dict[str, Any]],
                     report: Dict[str, Any]) -> "set[str]":
    """Suppress/remove/hide whatever a missing mirrored file stood for, or —
    if too many disappeared at once — trip the guard and touch nothing.
    Returns the sources the guard blocked, so the export phase that follows
    knows not to silently put those files right back this run."""
    from src.settings import get_setting

    ratio = float(get_setting("brain_vault_delete_guard_ratio", 0.3) or 0.3)
    mirrored_total = _count_sync_state(owner)
    threshold = max(5, ratio * mirrored_total)
    if len(missing_rows) > threshold:
        report["guard_tripped"] = True
        report["notes"].append(
            f"delete guard: {len(missing_rows)} mirrored files missing "
            f"(> {threshold:.0f} of {mirrored_total}); no deletions applied this run"
        )
        return {row["source"] for row in missing_rows}
    for row in missing_rows:
        source, path = row["source"], row["path"]
        try:
            if source.startswith("mem:"):
                _delete_mem(owner, source[4:], path, report)
            elif source.startswith("pmem:"):
                _delete_pmem(owner, source[5:], path, report)
            elif source.startswith("ent:"):
                _delete_ent(owner, source[4:], path, report)
            else:
                continue
        except Exception as exc:  # noqa: BLE001 - one bad row must not sink the run
            logger.exception("brain vault: deletion failed for %s", path)
            report["errors"].append(f"{path}: {exc}")
            continue
        _db_delete_sync_state(owner, path)
        _db_delete_notes_row(owner, path)
    return set()


def _delete_mem(owner: str, item_id: str, path: str, report: Dict[str, Any]) -> None:
    from src import memory_engine as engine

    item = engine.get_item(item_id)
    if not item:
        return
    content = render.render_memory_note(item, entities=_entity_links_for(f"mem:{item_id}"))
    _trash_add(owner, path, render.memory_title_text(item), f"mem:{item_id}", content, "suppressed")
    engine.set_suppressed(item_id, True)
    report["suppressed"] += 1


def _delete_pmem(owner: str, entry_id: str, path: str, report: Dict[str, Any]) -> None:
    from src import memory as memmod

    mgr = memmod.MemoryManager(_personal_data_dir())
    try:
        entries = mgr.load_all_for_update()
    except memmod.MemoryStoreUnreadable as exc:
        report["errors"].append(f"{path}: personal store unreadable, deletion skipped ({exc})")
        return
    target = next((e for e in entries if str(e.get("id")) == entry_id), None)
    if target is None:
        return
    content = render.render_personal_note(target)
    mgr.save([e for e in entries if str(e.get("id")) != entry_id])
    _trash_add(owner, path, str(target.get("text") or "")[:80], f"pmem:{entry_id}", content, "removed")
    report["suppressed"] += 1


def _delete_ent(owner: str, entity_id: str, path: str, report: Dict[str, Any]) -> None:
    ent = _entities()
    if ent is None:
        report["errors"].append(f"{path}: entities module unavailable, deletion skipped")
        return
    entity = ent.get_entity(entity_id)
    if not entity:
        return
    try:
        profile = ent.profile(entity_id)
    except Exception:  # noqa: BLE001
        profile = {}
    content = render.render_entity_note(entity, profile)
    ent.set_hidden(entity_id, True)
    _trash_add(owner, path, str(entity.get("name") or ""), f"ent:{entity_id}", content, "hidden")
    report["suppressed"] += 1


# ── the public entry points ──────────────────────────────────────────────

def sync(owner: str = "", *, budget_s: float = 30.0) -> Dict[str, Any]:
    """Import changed files, export the stores to files, then reindex.

    A human edit always wins: any mirrored file whose content changed since
    the last sync is folded into its store BEFORE that store is re-rendered
    back out, so the file the human just wrote is what ends up on disk
    (modulo pretty-printing) rather than being clobbered by the old data.
    """
    owner = str(owner or "")
    start = time.time()
    report: Dict[str, Any] = {
        "at": db.now_iso(), "exported": 0, "imported": 0, "created": 0,
        "suppressed": 0, "conflicts": [], "guard_tripped": False, "errors": [],
        "duration_ms": 0, "notes": [],
    }
    root = vault_root(owner)
    os.makedirs(root, exist_ok=True)

    # -- 1. import: a snapshot of what was mirrored before this run, so a
    # file present at the start but gone now is a deletion, not noise from
    # something this very sync is about to create.
    known_sync = _all_sync_state(owner)
    missing = [row for row in known_sync.values() if not os.path.exists(abs_path(owner, row["path"]))]

    for rel_path, full in _iter_files(root):
        if time.time() - start > budget_s:
            report["notes"].append("import phase stopped early: time budget exceeded")
            break
        try:
            text = _read(full)
            if text is None:
                continue
            fm_data, _ = fm.split(text)
            source = str(fm_data.get("source") or "")
            prior_hash = known_sync.get(rel_path, {}).get("file_hash", "")
            file_hash = db.sha(text)
            if source.startswith("mem:") and file_hash != prior_hash:
                _process_mem_file(owner, rel_path, text, source[4:], report)
                report["imported"] += 1
            elif source.startswith("pmem:") and file_hash != prior_hash:
                _process_pmem_file(owner, rel_path, text, source[5:], report)
                report["imported"] += 1
            elif source.startswith("ent:") and file_hash != prior_hash:
                _process_ent_file(owner, rel_path, text, source[4:], report)
                report["imported"] += 1
            elif not source and rel_path.startswith("Memories/") and rel_path not in known_sync:
                if _process_new_memory(owner, rel_path, text, report):
                    report["created"] += 1
                    report["imported"] += 1
        except Exception as exc:  # noqa: BLE001 - one bad file must not sink the run
            logger.exception("brain vault: import failed for %s", rel_path)
            report["errors"].append(f"{rel_path}: {exc}")

    # -- 2. deletions (guarded)
    skip_sources: "set[str]" = set()
    if missing:
        skip_sources = _apply_deletions(owner, missing, report)

    # -- 3. export: DB -> files (skipping whatever the guard just blocked,
    # so a mass deletion the guard refused to apply is not silently undone
    # by putting the files right back this same run)
    known_by_source = _known_paths_by_source(owner)
    projects_list: List[Dict[str, Any]] = []
    for label, action in (
        ("memories", lambda: _export_memories(owner, known_by_source, report, skip_sources)),
        ("personal", lambda: _export_personal(owner, known_by_source, report, skip_sources)),
        ("entities", lambda: _export_entities(owner, known_by_source, report, skip_sources)),
    ):
        try:
            action()
        except Exception as exc:  # noqa: BLE001
            logger.exception("brain vault: export %s failed", label)
            report["errors"].append(f"export {label}: {exc}")

    try:
        projects_list = _projects_for_owner(owner)
        _export_projects(owner, projects_list, known_by_source, report)
    except Exception as exc:  # noqa: BLE001
        logger.exception("brain vault: export projects failed")
        report["errors"].append(f"export projects: {exc}")

    try:
        _export_home(owner, projects_list, known_by_source, report)
    except Exception as exc:  # noqa: BLE001
        logger.exception("brain vault: export home failed")
        report["errors"].append(f"export home: {exc}")

    # -- 4. reindex (search/graph/backlinks over the vault as it now stands)
    try:
        from . import notes as notes_mod
        notes_mod.reindex(owner)
    except Exception as exc:  # noqa: BLE001
        logger.exception("brain vault: reindex failed")
        report["errors"].append(f"reindex: {exc}")

    report["duration_ms"] = int((time.time() - start) * 1000)
    _record_run(owner, report)
    return report


def import_file(owner: str, rel_path: str) -> Dict[str, Any]:
    """Import ONE file right now (used right after `notes.write_note`), then
    re-export just that source and reindex — a human should not have to wait
    for the next periodic sync to see their own edit take effect."""
    owner = str(owner or "")
    rel_path = validate_path(rel_path)
    text = _read(abs_path(owner, rel_path))
    if text is None:
        return {"action": "missing", "path": rel_path}

    fm_data, _ = fm.split(text)
    source = str(fm_data.get("source") or "")
    scratch: Dict[str, Any] = {"errors": []}
    action = "noop"
    result_source = source

    if source.startswith("mem:"):
        item = _process_mem_file(owner, rel_path, text, source[4:], scratch)
        action = "updated" if item else "error"
        result_source = f"mem:{item['id']}" if item else source
    elif source.startswith("pmem:"):
        entry = _process_pmem_file(owner, rel_path, text, source[5:], scratch)
        action = "updated" if entry else "error"
    elif source.startswith("ent:"):
        entity = _process_ent_file(owner, rel_path, text, source[4:], scratch)
        action = "updated" if entity else "error"
    elif not source and rel_path.startswith("Memories/"):
        created = _process_new_memory(owner, rel_path, text, scratch)
        action = "created" if created else "noop"
        result_source = f"mem:{created['id']}" if created else ""

    result: Dict[str, Any] = {
        "action": action, "source": result_source, "errors": scratch["errors"], "path": rel_path,
    }
    if result_source:
        new_path = _export_single(owner, result_source)
        if new_path:
            result["path"] = new_path

    try:
        from . import notes as notes_mod
        notes_mod.reindex(owner)
    except Exception as exc:  # noqa: BLE001
        result["errors"] = list(result["errors"]) + [f"reindex: {exc}"]
    return result
