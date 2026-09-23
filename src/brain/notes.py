"""
brain/notes.py — the notes API: tree, read/write/create/rename/delete,
full-text search, the link graph, tags, backlinks and the daily note.

Everything here operates on vault-relative paths (validated by
`vault.validate_path`) and the `notes`/`links`/`notes_fts`/`trash` tables
`vault.py` owns the schema of. `reindex()` is the one function that turns
what is actually on disk into those rows — `vault.sync()` calls it last, and
`write_note`/`create_note`/`rename_note`/`delete_note` each call it too (or
the cheaper `vault.import_file`, which ends with a reindex) so the index is
never stale after an API call returns.
"""

from __future__ import annotations

import contextlib
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from . import db, frontmatter as fm, render, vault, wikilinks


# ── indexing ─────────────────────────────────────────────────────────────

def reindex(owner: str = "") -> int:
    """Rebuild `notes`/`links`/`notes_fts` from what is actually on disk.
    Returns the number of notes indexed. Idempotent and safe to call often —
    `vault.sync()` calls it after every import+export pass."""
    owner = str(owner or "")
    root = vault.vault_root(owner)
    os.makedirs(root, exist_ok=True)

    found: Dict[str, Dict[str, Any]] = {}
    for rel_path, full in vault._iter_files(root):
        text = vault._read(full)
        if text is None:
            continue
        fm_data, body = fm.split(text)
        title = str(fm_data.get("title") or vault._title_from_path(rel_path))
        kind = str(fm_data.get("kind") or "note")
        source = str(fm_data.get("source") or "")
        try:
            stat = os.stat(full)
            mtime, size = stat.st_mtime, stat.st_size
        except OSError:
            mtime, size = 0.0, len(text.encode("utf-8"))
        found[rel_path] = {
            "title": title, "kind": kind, "source": source, "hash": db.sha(text),
            "mtime": mtime, "size": size, "frontmatter": fm_data,
            "tags": wikilinks.tags(body, fm_data), "body": body,
        }

    title_to_path: Dict[str, str] = {}
    fold_to_path: Dict[str, str] = {}
    for path, info in found.items():
        title_to_path.setdefault(info["title"], path)
        fold_to_path.setdefault(db.fold(info["title"]), path)

    with db.db() as conn:
        existing = {r["path"] for r in conn.execute(
            "SELECT path FROM notes WHERE owner=?", (owner,)
        ).fetchall()}
        for path in existing - set(found):
            _wipe_path(conn, owner, path)

        now = db.now_iso()
        for path, info in found.items():
            conn.execute(
                "INSERT INTO notes (owner, path, title, kind, source, hash, mtime, size, "
                "frontmatter, tags, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(owner, path) DO UPDATE SET title=excluded.title, kind=excluded.kind, "
                "source=excluded.source, hash=excluded.hash, mtime=excluded.mtime, "
                "size=excluded.size, frontmatter=excluded.frontmatter, tags=excluded.tags, "
                "updated_at=excluded.updated_at",
                (owner, path, info["title"], info["kind"], info["source"], info["hash"],
                 info["mtime"], info["size"], db.dumps(info["frontmatter"]),
                 db.dumps(info["tags"]), now),
            )
            conn.execute("DELETE FROM notes_fts WHERE owner=? AND path=?", (owner, path))
            conn.execute(
                "INSERT INTO notes_fts (owner, path, title, body) VALUES (?,?,?,?)",
                (owner, path, db.fold(info["title"]), db.fold(info["body"])),
            )
            conn.execute("DELETE FROM links WHERE owner=? AND src_path=?", (owner, path))
            for link in wikilinks.parse(info["body"]):
                target = link["target"]
                dst = title_to_path.get(target) or fold_to_path.get(db.fold(target))
                conn.execute(
                    "INSERT INTO links (owner, src_path, target, dst_path, label, is_embed) "
                    "VALUES (?,?,?,?,?,?)",
                    (owner, path, target, dst, link["label"], 1 if link["is_embed"] else 0),
                )
    return len(found)


def _wipe_path(conn: sqlite3.Connection, owner: str, path: str) -> None:
    conn.execute("DELETE FROM notes WHERE owner=? AND path=?", (owner, path))
    conn.execute("DELETE FROM links WHERE owner=? AND src_path=?", (owner, path))
    conn.execute("DELETE FROM notes_fts WHERE owner=? AND path=?", (owner, path))
    conn.execute("DELETE FROM user_zones WHERE owner=? AND path=?", (owner, path))


# ── reads ────────────────────────────────────────────────────────────────

def tree(owner: str) -> Dict[str, Any]:
    owner = str(owner or "")
    with db.db() as conn:
        rows = conn.execute(
            "SELECT path, title, kind, source, updated_at, size FROM notes "
            "WHERE owner=? ORDER BY path", (owner,),
        ).fetchall()
    folders: set = set()
    note_list = []
    for row in rows:
        note_list.append({
            "path": row["path"], "title": row["title"], "kind": row["kind"],
            "source": row["source"], "updated_at": row["updated_at"], "size": row["size"],
        })
        parts = row["path"].split("/")[:-1]
        acc: List[str] = []
        for part in parts:
            acc.append(part)
            folders.add("/".join(acc))
    return {"folders": sorted(folders), "notes": note_list}


def read_note(owner: str, path: str) -> Dict[str, Any]:
    owner = str(owner or "")
    rel_path = vault.validate_path(path)
    text = vault._read(vault.abs_path(owner, rel_path))
    if text is None:
        raise FileNotFoundError(rel_path)
    fm_data, body = fm.split(text)
    title = str(fm_data.get("title") or vault._title_from_path(rel_path))
    kind = str(fm_data.get("kind") or "note")
    source = str(fm_data.get("source") or "")
    user_zone = render.user_zone_of(body)
    marker_idx = body.find(render.GENERATED_MARKER)
    generated = body[marker_idx + len(render.GENERATED_MARKER):].strip("\n") if marker_idx >= 0 else ""

    with db.db() as conn:
        link_rows = conn.execute(
            "SELECT target, dst_path, label FROM links WHERE owner=? AND src_path=?",
            (owner, rel_path),
        ).fetchall()
    links = [
        {"target": r["target"], "path": r["dst_path"], "resolved": r["dst_path"] is not None,
         "label": r["label"]}
        for r in link_rows
    ]

    return {
        "path": rel_path, "title": title, "kind": kind, "source": source,
        "frontmatter": fm_data, "user_zone": user_zone, "generated": generated,
        "content": text, "links": links, "backlinks": backlinks(owner, rel_path),
        "tags": wikilinks.tags(body, fm_data),
        "updated_at": str(fm_data.get("updated") or fm_data.get("created") or ""),
        "editable": True,
    }


def _context_line(body: str, target_title: str) -> str:
    needle = f"[[{target_title}"
    for line in body.splitlines():
        if needle in line:
            return line.strip()[:160]
    return ""


def backlinks(owner: str, path: str) -> List[Dict[str, Any]]:
    owner = str(owner or "")
    rel_path = vault.validate_path(path)
    target_title = vault._title_from_path(rel_path)
    with db.db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT l.src_path AS src_path, n.title AS title FROM links l "
            "LEFT JOIN notes n ON n.owner = l.owner AND n.path = l.src_path "
            "WHERE l.owner=? AND l.dst_path=?", (owner, rel_path),
        ).fetchall()
    out = []
    for row in rows:
        src = row["src_path"]
        text = vault._read(vault.abs_path(owner, src))
        context = _context_line(fm.split(text)[1], target_title) if text else ""
        out.append({"path": src, "title": row["title"] or src, "context": context})
    return sorted(out, key=lambda r: r["path"])


def tags(owner: str) -> List[Dict[str, Any]]:
    owner = str(owner or "")
    with db.db() as conn:
        rows = conn.execute("SELECT tags FROM notes WHERE owner=?", (owner,)).fetchall()
    counts: Dict[str, int] = {}
    for row in rows:
        for tag in db.loads(row["tags"], []):
            counts[tag] = counts.get(tag, 0) + 1
    return [{"tag": t, "count": c} for t, c in sorted(counts.items())]


def unresolved(owner: str) -> List[Dict[str, Any]]:
    owner = str(owner or "")
    with db.db() as conn:
        rows = conn.execute(
            "SELECT target, src_path FROM links WHERE owner=? AND dst_path IS NULL",
            (owner,),
        ).fetchall()
    grouped: Dict[str, List[str]] = {}
    for row in rows:
        grouped.setdefault(row["target"], []).append(row["src_path"])
    return [{"target": t, "from": sorted(set(paths))} for t, paths in sorted(grouped.items())]


def search(owner: str, q: str, *, limit: int = 20) -> List[Dict[str, Any]]:
    owner = str(owner or "")
    folded = db.fold(q)
    tokens = [t for t in folded.split() if t]
    if not tokens:
        return []
    match_expr = " ".join(f'"{t}"*' for t in tokens)
    with db.db() as conn:
        try:
            rows = conn.execute(
                "SELECT n.path AS path, n.title AS title, n.kind AS kind, "
                "bm25(notes_fts, 0.0, 0.0, 10.0, 1.0) AS rank, "
                "snippet(notes_fts, 3, '[', ']', ' ... ', 12) AS snip "
                "FROM notes_fts JOIN notes n ON n.owner = notes_fts.owner AND n.path = notes_fts.path "
                "WHERE notes_fts.owner=? AND notes_fts MATCH ? "
                "ORDER BY rank LIMIT ?",
                (owner, match_expr, max(1, min(200, int(limit or 20)))),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
    return [
        {"path": r["path"], "title": r["title"], "kind": r["kind"], "snippet": r["snip"],
         "score": round(-float(r["rank"]), 4)}
        for r in rows
    ]


def graph(owner: str, *, center: Optional[str] = None, depth: int = 1,
         kinds: Optional[Sequence[str]] = None, limit: int = 600) -> Dict[str, Any]:
    owner = str(owner or "")
    with db.db() as conn:
        note_rows = conn.execute(
            "SELECT path, title, kind, tags FROM notes WHERE owner=?", (owner,)
        ).fetchall()
        link_rows = conn.execute(
            "SELECT src_path, target, dst_path FROM links WHERE owner=?", (owner,)
        ).fetchall()

    kind_filter = set(kinds) if kinds else None
    nodes: Dict[str, Dict[str, Any]] = {}
    for row in note_rows:
        if kind_filter and row["kind"] not in kind_filter:
            continue
        nodes[row["path"]] = {
            "id": row["path"], "label": row["title"], "kind": row["kind"],
            "degree": 0, "tags": db.loads(row["tags"], []),
        }

    edges: List[Dict[str, Any]] = []
    adjacency: Dict[str, set] = {}
    for row in link_rows:
        src, dst, target = row["src_path"], row["dst_path"], row["target"]
        dst_id = dst if dst else f"unresolved:{target}"
        if dst_id not in nodes:
            if dst is None:
                nodes[dst_id] = {"id": dst_id, "label": target, "kind": "unresolved",
                                 "degree": 0, "tags": []}
            else:
                continue  # the destination note exists but was filtered out by kind
        if src not in nodes:
            continue
        edges.append({"from": src, "to": dst_id, "kind": "link"})
        nodes[src]["degree"] += 1
        nodes[dst_id]["degree"] += 1
        adjacency.setdefault(src, set()).add(dst_id)
        adjacency.setdefault(dst_id, set()).add(src)

    if center:
        center = vault.validate_path(center)
        keep = {center}
        frontier = {center}
        for _ in range(max(0, int(depth))):
            nxt: set = set()
            for node_id in frontier:
                nxt |= adjacency.get(node_id, set())
            nxt -= keep
            keep |= nxt
            frontier = nxt
        nodes = {k: v for k, v in nodes.items() if k in keep}
        edges = [e for e in edges if e["from"] in keep and e["to"] in keep]

    node_list = sorted(nodes.values(), key=lambda n: n["id"])[:limit]
    kept_ids = {n["id"] for n in node_list}
    edge_list = [e for e in edges if e["from"] in kept_ids and e["to"] in kept_ids]
    return {"nodes": node_list, "edges": edge_list}


# ── writes ───────────────────────────────────────────────────────────────

def write_note(owner: str, path: str, content: str) -> Dict[str, Any]:
    owner = str(owner or "")
    rel_path = vault.validate_path(path)
    vault._atomic_write(vault.abs_path(owner, rel_path), str(content or ""))
    applied = vault.import_file(owner, rel_path)
    final_path = applied.get("path") or rel_path
    return {"note": read_note(owner, final_path), "applied": applied}


def create_note(owner: str, title: str, *, folder: str = "Notes", content: str = "") -> Dict[str, Any]:
    owner = str(owner or "")
    folder = (folder or "Notes").strip("/") or "Notes"
    base_rel = vault.validate_path(f"{folder}/{db.safe_filename(title)}.md")[:-3]
    rel_path = f"{base_rel}.md"
    suffix = 2
    while vault._read(vault.abs_path(owner, rel_path)) is not None:
        rel_path = f"{base_rel} {suffix}.md"
        suffix += 1

    fields = {"kind": "note", "created": db.now_iso(), "updated": db.now_iso()}
    body = "\n" + str(content or "").strip("\n") + "\n" if content else "\n"
    vault._atomic_write(vault.abs_path(owner, rel_path), fm.join(fields, body))
    applied = vault.import_file(owner, rel_path)
    return read_note(owner, applied.get("path") or rel_path)


def rename_note(owner: str, path: str, new_title: str, *, update_links: bool = True) -> Dict[str, Any]:
    owner = str(owner or "")
    rel_path = vault.validate_path(path)
    full = vault.abs_path(owner, rel_path)
    text = vault._read(full)
    if text is None:
        raise FileNotFoundError(rel_path)
    fm_data, body = fm.split(text)
    source = str(fm_data.get("source") or "")
    old_title = vault._title_from_path(rel_path)
    new_title = str(new_title or "").strip()
    if not new_title:
        raise ValueError("new_title is required")

    if source.startswith("mem:"):
        raise ValueError(
            "a memory note's title is derived from its text; edit the text instead of renaming"
        )

    if source.startswith("ent:"):
        ent = vault._entities()
        if ent is None:
            raise ValueError("entities are not available in this build")
        entity_id = source[4:]
        if not ent.get_entity(entity_id):
            raise ValueError(f"unknown entity id {entity_id}")
        ent.update_entity(entity_id, name=new_title)
        new_path = vault._export_single(owner, source) or rel_path
    else:
        new_dirname = os.path.dirname(rel_path)
        new_filename = f"{db.safe_filename(new_title)}.md"
        new_path = vault.validate_path(f"{new_dirname}/{new_filename}" if new_dirname else new_filename)
        if new_path != rel_path:
            new_full = vault.abs_path(owner, new_path)
            if vault._read(new_full) is not None:
                raise ValueError(f"a note already exists at {new_path}")
            new_fields = dict(fm_data)
            if new_fields.get("title"):
                new_fields["title"] = new_title
            vault._atomic_write(new_full, fm.join(new_fields, body))
            with contextlib.suppress(OSError):
                os.remove(full)
            vault._db_delete_notes_row(owner, rel_path)
            if source:
                vault._db_delete_sync_state(owner, rel_path)

    updated_links = 0
    if update_links and old_title != new_title:
        with db.db() as conn:
            src_paths = [r["src_path"] for r in conn.execute(
                "SELECT DISTINCT src_path FROM links WHERE owner=? AND target=?",
                (owner, old_title),
            ).fetchall()]
        for src_path in src_paths:
            if src_path in (rel_path, new_path):
                continue
            src_full = vault.abs_path(owner, src_path)
            src_text = vault._read(src_full)
            if src_text is None:
                continue
            src_fields, src_body = fm.split(src_text)
            rewritten = wikilinks.rewrite_target(src_body, old_title, new_title)
            if rewritten != src_body:
                vault._atomic_write(src_full, fm.join(src_fields, rewritten))
                updated_links += 1

    reindex(owner)
    return {"note": read_note(owner, new_path), "updated_links": updated_links}


def delete_note(owner: str, path: str) -> Dict[str, Any]:
    owner = str(owner or "")
    rel_path = vault.validate_path(path)
    full = vault.abs_path(owner, rel_path)
    text = vault._read(full)
    if text is None:
        raise FileNotFoundError(rel_path)
    fm_data, _ = fm.split(text)
    source = str(fm_data.get("source") or "")
    title = str(fm_data.get("title") or vault._title_from_path(rel_path))

    effect = "removed"
    if source.startswith("mem:"):
        from src import memory_engine as engine
        item_id = source[4:]
        if engine.get_item(item_id):
            engine.set_suppressed(item_id, True)
        effect = "suppressed"
    elif source.startswith("pmem:"):
        from src import memory as memmod
        mgr = memmod.MemoryManager(vault._personal_data_dir())
        with contextlib.suppress(memmod.MemoryStoreUnreadable):
            entries = mgr.load_all_for_update()
            mgr.save([e for e in entries if str(e.get("id")) != source[5:]])
        effect = "removed"
    elif source.startswith("ent:"):
        ent = vault._entities()
        if ent is not None:
            ent.set_hidden(source[4:], True)
        effect = "hidden"

    trash_id = vault._trash_add(owner, rel_path, title, source, text, effect)
    with contextlib.suppress(OSError):
        os.remove(full)
    vault._db_delete_notes_row(owner, rel_path)
    if source:
        vault._db_delete_sync_state(owner, rel_path)
    return {"trash_id": trash_id, "effect": effect}


def list_trash(owner: str) -> List[Dict[str, Any]]:
    owner = str(owner or "")
    with db.db() as conn:
        rows = conn.execute(
            "SELECT id, path, title, source, effect, deleted_at FROM trash "
            "WHERE owner=? AND restored_at IS NULL ORDER BY deleted_at DESC",
            (owner,),
        ).fetchall()
    return [dict(r) for r in rows]


def restore(owner: str, trash_id: str) -> Dict[str, Any]:
    owner = str(owner or "")
    with db.db() as conn:
        row = conn.execute(
            "SELECT * FROM trash WHERE id=? AND owner=?", (trash_id, owner)
        ).fetchone()
    if not row:
        raise ValueError(f"unknown trash id {trash_id}")
    source = row["source"] or ""

    if source.startswith("mem:"):
        from src import memory_engine as engine
        item_id = source[4:]
        if not engine.get_item(item_id):
            raise ValueError("the underlying memory no longer exists (it may have been forgotten since)")
        engine.set_suppressed(item_id, False)
    elif source.startswith("pmem:"):
        from src import memory as memmod
        mgr = memmod.MemoryManager(vault._personal_data_dir())
        entries = mgr.load_all_for_update()
        entry_id = source[5:]
        if not any(str(e.get("id")) == entry_id for e in entries):
            _, body = fm.split(row["content"])
            entries.append({
                "id": entry_id, "text": render.user_zone_of(body).strip(),
                "timestamp": int(time.time()), "source": "restored", "category": "fact",
                "owner": owner or None,
            })
            mgr.save(entries)
    elif source.startswith("ent:"):
        ent = vault._entities()
        if ent is None:
            raise ValueError("entities are not available in this build")
        ent.set_hidden(source[4:], False)

    with db.db() as conn:
        conn.execute("UPDATE trash SET restored_at=? WHERE id=?", (db.now_iso(), trash_id))

    if source:
        new_path = vault._export_single(owner, source) or row["path"]
    else:
        new_path = row["path"]
        vault._atomic_write(vault.abs_path(owner, new_path), row["content"])
    reindex(owner)
    return read_note(owner, new_path)


def daily_note(owner: str, date: Optional[str] = None) -> Dict[str, Any]:
    owner = str(owner or "")
    date_str = str(date or "").strip() or datetime.now(timezone.utc).date().isoformat()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_str):
        raise ValueError("date must be YYYY-MM-DD")
    rel_path = f"Daily/{date_str}.md"
    if vault._read(vault.abs_path(owner, rel_path)) is None:
        vault._atomic_write(
            vault.abs_path(owner, rel_path),
            render.render_daily_note(date_str, learned=(), user_zone=""),
        )
        reindex(owner)
    return read_note(owner, rel_path)
