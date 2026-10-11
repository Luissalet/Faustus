"""brain/fact_history.py - the change trace of one fact (subject + attribute).

No new store: a fact is a row of ``relations`` and the brain already closes the
old row of a single-valued attribute (``works_at``, ``lives_in``,
``located_in``) when a new value arrives (``status = superseded``,
``valid_until``, ``superseded_by``). This module reads those rows as an event
chain so "where does Ada work?" can be answered with the current value and a
short history instead of several loose memories:

* every value is an *event* with its validity window, its evidence refs and
  the session it was learned in (best effort, from the learned-memory row);
* events are linked both ways, ``supersedes`` (the event this one replaced)
  and ``superseded_by`` (the event that replaced it), derived from the stored
  ``superseded_by`` pointer;
* :func:`history_line` renders the short line used at injection time
  ("antes: Cordera Labs, hasta 2026-09").

:func:`attributes_in_text` finds which attribute a question is about, so the
history is only injected when the question asks for it. Pure reads, owner
scoped, nothing is deleted or rewritten.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Sequence

from src.brain.db import fold

#: Question wording that names an attribute without using the relation verb.
_QUESTION_PATTERNS = (
    (re.compile(r"\bdonde (?:trabaj|labor)"), "works_at"),
    (re.compile(r"\bpara quien trabaj"), "works_at"),
    (re.compile(r"\bwhere (?:does|did|do|is)\b.*\bwork"), "works_at"),
    (re.compile(r"\bwho (?:does|did)\b.*\bwork for\b"), "works_at"),
    (re.compile(r"\bdonde viv"), "lives_in"),
    (re.compile(r"\bwhere (?:does|did|do)\b.*\blive"), "lives_in"),
    (re.compile(r"\bdonde (?:esta|se encuentra)\b"), "located_in"),
    (re.compile(r"\bwhere is\b"), "located_in"),
    (re.compile(r"\b(?:empleo|empleador|employer)\b"), "works_at"),
)

#: Single words too ambiguous to count as naming an attribute on their own.
_AMBIGUOUS_PHRASES = frozenset({"use", "uso", "own", "know", "creo", "created", "using"})


def _label(relation: Dict[str, Any]) -> str:
    from src.brain import entities
    if relation.get("dst"):
        entity = entities.get_entity(relation["dst"])
        if entity:
            return str(entity.get("name") or "")
    return str(relation.get("dst_value") or "")


def _session_of(refs: Sequence[str]) -> str:
    for ref in refs:
        if str(ref).startswith("mem:"):
            try:
                from src import memory_engine
                item = memory_engine.get_item(str(ref)[4:]) or {}
            except Exception:  # noqa: BLE001 - provenance is best effort
                return ""
            session = str(item.get("session_id") or "")
            if session:
                return session
    return ""


def _event(relation: Dict[str, Any], supersedes: str) -> Dict[str, Any]:
    refs = list(relation.get("evidence") or [])
    return {
        "id": relation["id"],
        "value": _label(relation),
        "dst": relation.get("dst") or "",
        "valid_from": relation.get("valid_from") or "",
        "valid_until": relation.get("valid_until") or "",
        "recorded_at": relation.get("asserted_at") or "",
        "status": relation.get("status") or "active",
        "supersedes": supersedes,
        "superseded_by": relation.get("superseded_by") or "",
        "evidence": refs,
        "source": refs[0] if refs else "",
        "source_session": _session_of(refs),
        "method": relation.get("method") or "",
        "confidence": relation.get("confidence"),
    }


def _is_current(relation: Dict[str, Any], now_iso: str) -> bool:
    if relation.get("status") != "active":
        return False
    until = str(relation.get("valid_until") or "")
    return not until or until > now_iso


def _short_date(value: str) -> str:
    return str(value or "")[:7]


def history_line(events: Sequence[Dict[str, Any]], *, max_items: int = 3) -> str:
    """"antes: X, hasta 2026-09" for the closed events, newest first; empty
    when nothing was replaced."""
    closed = [e for e in events if e.get("status") != "active" or e.get("valid_until")]
    closed = [e for e in closed if e.get("value")]
    closed.sort(key=lambda e: (e.get("valid_until") or e.get("recorded_at") or ""), reverse=True)
    parts = []
    for event in closed[:max(1, max_items)]:
        until = _short_date(event.get("valid_until"))
        parts.append(f"{event['value']}, hasta {until}" if until else str(event["value"]))
    return ("antes: " + "; ".join(parts)) if parts else ""


def fact_history(owner: Any, subject: Any, attribute: Any, *, limit: int = 20) -> Dict[str, Any]:
    """Current value and change chain of ``subject``'s ``attribute``.

    ``subject`` is an entity id or a name/alias; ``attribute`` any wording
    :func:`entities.canonical_relation` knows ("works at", "trabaja en") or a
    free relation key. Raises ``ValueError`` for an unknown subject or an
    empty attribute."""
    from src.brain import entities
    from src.brain.db import now_iso

    owner = str(owner or "")
    key = entities.canonical_relation(attribute) or entities._normalize_rel(attribute)
    if not key:
        raise ValueError("attribute is required")
    ref = str(subject or "").strip()
    if not ref:
        raise ValueError("subject is required")
    entity = entities.get_entity(ref)
    if entity and entity.get("owner") != owner:
        entity = None
    entity = entity or entities.find_entity(owner, ref)
    if not entity:
        raise ValueError(f"unknown subject {ref!r}")

    rows = [r for r in entities.list_relations(owner, entity_id=entity["id"], include_closed=True)
            if r["src"] == entity["id"] and r["rel"] == key]
    rows.sort(key=lambda r: (r.get("valid_from") or r.get("asserted_at") or "", r.get("created_at") or ""))
    predecessor = {r["superseded_by"]: r["id"] for r in rows if r.get("superseded_by")}
    events = [_event(r, predecessor.get(r["id"], "")) for r in rows]
    stamp = now_iso()
    current = [e for e, r in zip(events, rows) if _is_current(r, stamp)]
    events = events[-max(1, int(limit)):]
    return {
        "subject": {"id": entity["id"], "name": entity["name"]},
        "attribute": key,
        "current": current,
        "events": events,
        "history_line": history_line(events),
        "changes": sum(1 for e in events if e["supersedes"] or e["superseded_by"]),
    }


def attributes_in_text(text: Any) -> List[str]:
    """Relation keys the text asks about (verb wording or question form)."""
    from src.brain import entities
    folded = fold(re.sub(r"[_\-?!,.;:¿¡]+", " ", str(text or "")))
    folded = " ".join(folded.split())
    found: List[str] = []
    for pattern, rel in _QUESTION_PATTERNS:
        if pattern.search(folded) and rel not in found:
            found.append(rel)
    for phrase, rel in entities._RELATION_SYNONYMS.items():
        if phrase in _AMBIGUOUS_PHRASES or rel in found:
            continue
        if re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", folded):
            found.append(rel)
    return found


def history_lines_for(owner: Any, entity_id: str, text: Any) -> List[str]:
    """Lines "<attribute>: <current> (antes: ...)" for each attribute the text
    asks about and that actually changed; empty otherwise."""
    lines: List[str] = []
    for key in attributes_in_text(text):
        try:
            view = fact_history(owner, entity_id, key)
        except ValueError:
            continue
        if not view["history_line"]:
            continue
        now = "; ".join(e["value"] for e in view["current"] if e["value"]) or "sin valor actual"
        lines.append(f"{key.replace('_', ' ')}: {now} ({view['history_line']})")
    return lines


__all__ = ["fact_history", "history_line", "attributes_in_text", "history_lines_for"]
