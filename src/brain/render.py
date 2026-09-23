"""
brain/render.py — one pure function per note kind, from source data to the
full file text the vault writes to disk.

Pure on purpose (no DB reads, no filesystem, no imports of `entities`): every
piece of data a section needs is passed in already fetched, so a render
function is a straight input -> output map a test can golden-check, and so
`vault.sync` can write a file only when this text actually changed (it
re-renders unconditionally and diffs the result, never the other way round).

Every generated note is: YAML frontmatter, the human's free "user zone" text,
the line `GENERATED_MARKER`, then the generated sections. Re-rendering keeps
the user zone byte-for-byte (`vault.sync` reads it back out of the existing
file before calling here) and replaces everything after the marker.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Sequence

from . import frontmatter as fm

GENERATED_MARKER = "%% faustus:generated — edits below this line are replaced on the next sync %%"

#: Folder name for each `memory_engine` item type (TYPES in the contract).
TYPE_FOLDERS: Dict[str, str] = {
    "preference": "Preferences",
    "fact": "Facts",
    "procedure": "Procedures",
    "decision": "Decisions",
    "anti_pattern": "Anti-patterns",
}


def _link(title: str, label: str = "") -> str:
    title = str(title or "").strip()
    label = str(label or "").strip()
    return f"[[{title}|{label}]]" if label and label != title else f"[[{title}]]"


def _bullets(lines: Iterable[str]) -> str:
    lines = [ln for ln in lines if ln]
    return "\n".join(f"- {ln}" for ln in lines)


def _section(heading: str, body: str) -> str:
    body = (body or "").strip()
    return f"## {heading}\n\n{body}" if body else f"## {heading}\n\n*(nada por ahora)*"


def user_zone_of(body: Any) -> str:
    """The editable text above `GENERATED_MARKER` in a note body (the part a
    re-render must preserve). No marker at all -> the whole body is the
    user zone (a free note, or one nobody has synced yet)."""
    text = str(body or "")
    idx = text.find(GENERATED_MARKER)
    return (text if idx < 0 else text[:idx]).strip("\n")


def compose(frontmatter_fields: Dict[str, Any], user_zone: str, generated: str) -> str:
    """Frontmatter + user zone + marker + generated sections, in that shape."""
    user_zone = (user_zone or "").rstrip("\n")
    generated = (generated or "").rstrip("\n")
    body = "\n" + (user_zone + "\n\n" if user_zone else "") + GENERATED_MARKER + "\n"
    if generated:
        body += "\n" + generated + "\n"
    return fm.join(frontmatter_fields, body)


# ── memory notes ────────────────────────────────────────────────────────

_WORD_RE = re.compile(r"\S+")


def memory_title_text(item: Dict[str, Any], *, limit: int = 60) -> str:
    """A short, deterministic label for a memory's filename: the first
    words of its text, cut at a word boundary."""
    text = " ".join(str(item.get("text") or "").split())
    if len(text) <= limit:
        return text or "memoria"
    cut = text[:limit]
    last_space = cut.rfind(" ")
    return (cut[:last_space] if last_space > 10 else cut).rstrip(",.;: ") + "..."


def _validity_state(item: Dict[str, Any]) -> str:
    until = str(item.get("valid_until") or "")
    return "cerrada" if until else "vigente"


def render_memory_note(
    item: Dict[str, Any],
    *,
    entities: Optional[Sequence[Dict[str, Any]]] = None,
    user_zone: str = "",
) -> str:
    fields = {
        "id": item.get("id"),
        "source": f"mem:{item.get('id')}",
        "kind": "memory",
        "type": item.get("type"),
        "level": item.get("level"),
        "status": item.get("status"),
        "trust": item.get("trust_class"),
        "confidence": item.get("confidence"),
        "valid_from": item.get("valid_from") or None,
        "valid_until": item.get("valid_until") or None,
        "created": item.get("created_at"),
        "updated": item.get("updated_at"),
        "project": item.get("project") or None,
        "tags": [item.get("category")] if item.get("category") else [],
        "pinned": bool(item.get("pinned")),
    }
    entity_lines = [_link(e.get("name", "")) for e in (entities or []) if e.get("name")]
    project = item.get("project") or ""
    project_body = _link(project) if project else ""
    evidence_lines = []
    for span in item.get("evidence") or []:
        if isinstance(span, dict):
            ref = span.get("ref") or span.get("session_id") or span.get("text") or ""
            evidence_lines.append(str(ref))
        elif span:
            evidence_lines.append(str(span))
    validity = (
        f"- Desde: {item.get('valid_from') or '(sin fecha)'}\n"
        f"- Hasta: {item.get('valid_until') or '(abierta)'}\n"
        f"- Estado: {_validity_state(item)}"
    )
    history_lines = []
    corrected_from = (item.get("provenance") or {}).get("corrected_from")
    if corrected_from:
        history_lines.append(f"Corrige a `mem:{corrected_from}`.")
    generated = "\n\n".join([
        _section("Entities", _bullets(entity_lines)),
        _section("Project", project_body),
        _section("Evidence", _bullets(evidence_lines)),
        _section("Validity", validity),
        _section("History", _bullets(history_lines)),
    ])
    return compose(fields, user_zone if user_zone else str(item.get("text") or ""), generated)


# ── personal notes ──────────────────────────────────────────────────────

def render_personal_note(entry: Dict[str, Any], *, user_zone: str = "") -> str:
    fields = {
        "id": entry.get("id"),
        "source": f"pmem:{entry.get('id')}",
        "kind": "personal",
        "type": entry.get("category") or "fact",
        "created": entry.get("timestamp"),
        "tags": [entry.get("category")] if entry.get("category") else [],
    }
    generated = _section("Detalles", f"- Fuente: {entry.get('source') or 'desconocida'}\n"
                                      f"- Usos: {int(entry.get('uses') or 0)}")
    return compose(fields, user_zone if user_zone else str(entry.get("text") or ""), generated)


# ── entity notes (data supplied by Lot B; degrade to a stub if absent) ──

def render_entity_note(
    entity: Dict[str, Any],
    profile: Optional[Dict[str, Any]] = None,
    *,
    user_zone: str = "",
) -> str:
    profile = profile or {}
    fields = {
        "id": entity.get("id"),
        "source": f"ent:{entity.get('id')}",
        "kind": "entity",
        "type": entity.get("type") or "other",
        "created": entity.get("created_at"),
        "updated": entity.get("updated_at"),
        "project": entity.get("project") or None,
        "aliases": list(entity.get("aliases") or []),
    }
    # Cited as `mem:<id8>` plain text, not a [[wikilink]]: a fact's source_ref
    # only carries the memory's id, not the title text its filename needs, so
    # a link built from it could never resolve and would just clutter the
    # unresolved-links list. Lot C/D can turn this into a real link once it
    # has the id -> path lookup (`notes.tree` gives it that mapping).
    facts = profile.get("facts") or []
    fact_lines = []
    for f in facts:
        ref = str(f.get("source_ref") or "")
        mem_id = ref.split(":", 1)[1] if ref.startswith("mem:") else ref
        citation = f"`mem:{mem_id[:8]}`" if mem_id else ""
        text = str(f.get("text") or "").strip()
        fact_lines.append(f"{text} ({citation})" if text and citation else text)

    relations = [r for r in (profile.get("relations") or []) if r.get("valid_at", True)]
    rel_lines = [
        f"{r.get('rel')} → {_link(r.get('dst_name') or r.get('dst_value') or '')}"
        for r in relations
    ]
    history = profile.get("history") or []
    hist_lines = [
        f"{h.get('rel')} → {h.get('dst_name') or h.get('dst_value') or ''} "
        f"({h.get('valid_from') or '?'} → {h.get('valid_until') or '?'})"
        for h in history
    ]
    mentions = profile.get("timeline") or []
    mention_lines = [str(m.get("text") or m.get("source_ref") or "") for m in mentions]

    generated = "\n\n".join([
        _section("Facts", _bullets(fact_lines)),
        _section("Relations", _bullets(rel_lines)),
        _section("History", _bullets(hist_lines)),
        _section("Mentioned in", _bullets(mention_lines)),
    ])
    default_zone = user_zone if user_zone else str(entity.get("summary") or profile.get("summary") or "")
    return compose(fields, default_zone, generated)


# ── project / objective / concept notes ─────────────────────────────────

def render_project_note(
    project: Dict[str, Any],
    *,
    memories: Sequence[Dict[str, Any]] = (),
    objectives: Sequence[Dict[str, Any]] = (),
    concepts: Sequence[Dict[str, Any]] = (),
    entities: Sequence[Dict[str, Any]] = (),
    user_zone: str = "",
) -> str:
    fields = {
        "id": project.get("id"),
        "source": f"proj:{project.get('id')}",
        "kind": "project",
        "created": project.get("created_at"),
        "updated": project.get("updated_at"),
    }
    mem_lines = [_link(memory_title_text(m) + f" ({str(m.get('id'))[:8]})") for m in memories]
    obj_lines = [_link(str(o.get("title") or o.get("id") or "")) for o in objectives]
    concept_lines = [_link(str(c.get("name") or "")) for c in concepts]
    entity_lines = [_link(str(e.get("name") or "")) for e in entities]
    generated = "\n\n".join([
        _section("Memories", _bullets(mem_lines)),
        _section("Objectives", _bullets(obj_lines)),
        _section("Concepts", _bullets(concept_lines)),
        _section("Entities", _bullets(entity_lines)),
    ])
    return compose(fields, user_zone, generated)


def render_objective_note(objective: Dict[str, Any], project_name: str, *, user_zone: str = "") -> str:
    fields = {
        "id": objective.get("id"),
        "source": f"obj:{project_name}/{objective.get('id')}",
        "kind": "objective",
        "status": objective.get("status"),
        "project": project_name or None,
    }
    body = (
        f"- Estado: {objective.get('status') or 'open'}\n"
        f"- Prioridad: P{objective.get('priority', 3)}\n"
        f"- Proyecto: {_link(project_name)}"
    )
    deps = objective.get("deps") or []
    if deps:
        body += "\n- Depende de: " + ", ".join(str(d) for d in deps)
    generated = _section("Detalles", body)
    return compose(fields, user_zone, generated)


def render_concept_note(concept: Dict[str, Any], project_name: str, *, user_zone: str = "") -> str:
    fields = {
        "id": concept.get("id"),
        "source": f"concept:{project_name}/{concept.get('id')}",
        "kind": "concept",
        "type": concept.get("kind"),
        "project": project_name or None,
        "created": concept.get("created_at"),
        "updated": concept.get("updated_at"),
    }
    body = str(concept.get("details") or concept.get("summary") or "")
    refs = concept.get("refs") or []
    generated = "\n\n".join([
        _section("Detalles", body),
        _section("Referencias", _bullets(str(r) for r in refs)),
        _section("Proyecto", _link(project_name)) if project_name else "",
    ])
    return compose(fields, user_zone, generated)


# ── daily and home ───────────────────────────────────────────────────────

def render_daily_note(date: str, *, learned: Sequence[str] = (), user_zone: str = "") -> str:
    fields = {
        "id": date,
        "source": None,
        "kind": "daily",
        "created": date,
    }
    generated = _section("Aprendido hoy", _bullets(learned))
    return compose(fields, user_zone, generated)


def render_home(
    *,
    projects: Sequence[Dict[str, Any]] = (),
    entity_counts: Sequence[Dict[str, Any]] = (),
    latest_memories: Sequence[Dict[str, Any]] = (),
    latest_daily: Sequence[str] = (),
    user_zone: str = "",
) -> str:
    fields = {"id": "home", "source": None, "kind": "home"}
    project_lines = [_link(str(p.get("name") or "")) for p in projects]
    entity_lines = [f"{c.get('type')}: {c.get('count')}" for c in entity_counts]
    memory_lines = [_link(memory_title_text(m) + f" ({str(m.get('id'))[:8]})") for m in latest_memories]
    daily_lines = [_link(d) for d in latest_daily]
    generated = "\n\n".join([
        _section("Projects", _bullets(project_lines)),
        _section("Entities", _bullets(entity_lines)),
        _section("Latest memories", _bullets(memory_lines)),
        _section("Daily", _bullets(daily_lines)),
    ])
    return compose(fields, user_zone, generated)
