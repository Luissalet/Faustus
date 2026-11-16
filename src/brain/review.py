"""brain/review.py — what needs the owner's eye: contradictions and expired content.

Two things pile up in a brain that agents write into:

* **Contradictions.** A functional relation (one employer, one home city, one
  location — :data:`entities.FUNCTIONAL_RELATIONS`) with two different values
  open at the same time. ``add_relation`` closes the outdated one when
  chronology says which it is; when it cannot (equal dates, a dated fact against
  an undated one, an owner-ratified fact against a newer claim) both stay open.
  :func:`contradictions` lists exactly those, ratified side first.
* **Expired content.** A relation whose validity window has ended but whose row
  still reads ``active``, and notes whose frontmatter says when they stop being
  true (``caduca`` / ``expires`` / ``valid_until``). :func:`expired` also lists
  what lapses within ``soon_days``.

:func:`resolve` is the quick action behind both lists: keep one side (ratify it
and archive the rest), archive, replace a value, or renew an expiry. Nothing is
deleted: an archived fact becomes ``retracted`` (kept for the record, out of the
graph) and an archived note goes to the vault trash, which can restore it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.brain import entities, ratified
from src.brain.db import db, fold, loads, now_iso, parse_iso

logger = logging.getLogger(__name__)

EXPIRY_KEYS = ("caduca", "expires", "valid_until")
ACTIONS = ("keep", "archive", "update", "renew")


class ReviewError(ValueError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _value(rel: Dict[str, Any]) -> str:
    return rel.get("dst") or fold(rel.get("dst_value"))


def _name(cache: Dict[str, str], entity_id: str) -> str:
    if entity_id and entity_id not in cache:
        entity = entities.get_entity(entity_id)
        cache[entity_id] = entity["name"] if entity else ""
    return cache.get(entity_id, "")


def _fact_label(rel: Dict[str, Any], names: Dict[str, str]) -> str:
    return (_name(names, rel.get("dst") or "") or rel.get("dst_value") or "").strip()


def _option(rel: Dict[str, Any], marks: Dict[str, Any], names: Dict[str, str]) -> Dict[str, Any]:
    return {
        "ref": f"rel:{rel['id']}", "relation_id": rel["id"], "value": _fact_label(rel, names),
        "valid_from": rel.get("valid_from") or "", "valid_until": rel.get("valid_until") or "",
        "asserted_at": rel.get("asserted_at") or "", "evidence": rel.get("evidence") or [],
        "confidence": rel.get("confidence", 0.0), "method": rel.get("method") or "rule",
        "ratified": f"rel:{rel['id']}" in marks,
    }


def contradictions(owner: Any, *, limit: int = 100) -> List[Dict[str, Any]]:
    """Functional relations with two or more different values open now."""
    owner = str(owner or "")
    now = _now()
    marks = ratified.refs(owner, "rel:")
    groups: Dict[tuple, List[Dict[str, Any]]] = {}
    for rel in entities.list_relations(owner, include_closed=False):
        if rel["rel"] not in entities.FUNCTIONAL_RELATIONS:
            continue
        if not entities._relation_window_covers(rel, now):
            continue
        groups.setdefault((rel["src"], rel["rel"]), []).append(rel)
    names: Dict[str, str] = {}
    out: List[Dict[str, Any]] = []
    for (src, rel_key), rels in groups.items():
        if len({_value(r) for r in rels}) < 2:
            continue
        entity = entities.get_entity(src)
        if not entity or entity.get("hidden") or entity.get("merged_into"):
            continue
        options = [_option(r, marks, names) for r in rels]
        options.sort(key=lambda o: (not o["ratified"], o["valid_from"]), reverse=False)
        winners = [o for o in options if o["ratified"]]
        out.append({
            "id": f"{src}:{rel_key}", "kind": "contradiction",
            "entity": {"id": src, "name": entity["name"], "type": entity["type"]},
            "rel": rel_key, "options": options,
            "settled_by_ratification": len(winners) == 1,
            "ratified_ref": winners[0]["ref"] if len(winners) == 1 else "",
        })
    out.sort(key=lambda c: (c["settled_by_ratification"], c["entity"]["name"]))
    return out[: max(1, int(limit or 100))]


def _expiry_of(front: Dict[str, Any]) -> Optional[str]:
    for key in EXPIRY_KEYS:
        value = front.get(key)
        if value not in (None, "", False):
            return str(value)
    return None


def expired(owner: Any, *, soon_days: int = 14, limit: int = 200) -> List[Dict[str, Any]]:
    """Facts and notes past their expiry (``state: "expired"``) or lapsing
    within `soon_days` (``state: "soon"``), most overdue first."""
    owner = str(owner or "")
    now = _now()
    horizon = now.timestamp() + max(0, int(soon_days)) * 86400
    marks = ratified.refs(owner)
    names: Dict[str, str] = {}
    out: List[Dict[str, Any]] = []

    for rel in entities.list_relations(owner, include_closed=False):
        until = parse_iso(rel.get("valid_until"))
        if until is None or until.timestamp() > horizon:
            continue
        entity = entities.get_entity(rel["src"])
        if not entity or entity.get("hidden") or entity.get("merged_into"):
            continue
        out.append({
            "id": f"rel:{rel['id']}", "kind": "fact", "ref": f"rel:{rel['id']}",
            "state": "expired" if until <= now else "soon",
            "expires_at": rel["valid_until"],
            "title": f"{entity['name']} · {rel['rel']} {_fact_label(rel, names)}".strip(),
            "entity": {"id": entity["id"], "name": entity["name"], "type": entity["type"]},
            "ratified": f"rel:{rel['id']}" in marks, "evidence": rel.get("evidence") or [],
        })

    from src.brain import vault  # noqa: F401  (registers the notes table)
    with db() as conn:
        rows = conn.execute("SELECT path, title, frontmatter FROM notes WHERE owner = ?",
                            (owner,)).fetchall()
    for row in rows:
        front = loads(row["frontmatter"], {}) or {}
        raw = _expiry_of(front) if isinstance(front, dict) else None
        until = parse_iso(raw) if raw else None
        if until is None or until.timestamp() > horizon:
            continue
        out.append({
            "id": f"note:{row['path']}", "kind": "note", "ref": f"note:{row['path']}",
            "state": "expired" if until <= now else "soon", "expires_at": str(raw),
            "title": row["title"], "path": row["path"],
            "ratified": f"note:{row['path']}" in marks, "evidence": [],
        })
    out.sort(key=lambda r: (r["state"] != "expired", r["expires_at"]))
    return out[: max(1, int(limit or 200))]


def overview(owner: Any, *, soon_days: int = 14) -> Dict[str, Any]:
    conflicts = contradictions(owner)
    stale = expired(owner, soon_days=soon_days)
    return {
        "contradictions": conflicts, "expired": stale,
        "counts": {
            "contradictions": len(conflicts),
            "unsettled": sum(1 for c in conflicts if not c["settled_by_ratification"]),
            "expired": sum(1 for s in stale if s["state"] == "expired"),
            "soon": sum(1 for s in stale if s["state"] == "soon"),
            "ratified": len(ratified.refs(owner)),
        },
        "functional_relations": sorted(entities.FUNCTIONAL_RELATIONS),
    }


# ── actions ────────────────────────────────────────────────────────────────


def _relation(owner: str, relation_id: str) -> Dict[str, Any]:
    with db() as conn:
        row = conn.execute("SELECT * FROM relations WHERE id = ?", (relation_id,)).fetchone()
    if row is None or (row["owner"] and row["owner"] != owner):
        raise ReviewError(f"fact not found: {relation_id}")
    return entities._row_to_relation(row)


def _retract(relation_id: str, now: str) -> None:
    entities._close_relation(relation_id, now, status="retracted", now=now)


def _touched(owner: str, *relations: Dict[str, Any]) -> List[str]:
    ids: List[str] = []
    for rel in relations:
        for key in ("src", "dst"):
            if rel.get(key) and rel[key] not in ids:
                ids.append(rel[key])
    return ids


def resolve(owner: Any, action: str, ref: str, *, value: str = "", until: Optional[str] = None,
            note: str = "") -> Dict[str, Any]:
    """One quick action on a contradiction option, an expired fact or a note.

    * ``keep``    — ratify `ref` (a ``rel:``) and archive the other open values
      of the same entity and relation;
    * ``archive`` — a fact becomes ``retracted``; a note goes to the vault trash;
    * ``update``  — `value` replaces the fact's value (the old one is closed as
      superseded, the new one is ratified);
    * ``renew``   — `until` (ISO date, ``""`` to clear) becomes the new expiry
      of the fact or note.

    Returns ``{"action", "ref", "changed": [...], "entities": [entity ids to
    re-mirror]}``; raises :class:`ReviewError` for anything it cannot do.
    """
    owner = str(owner or "")
    action = str(action or "").strip().lower()
    if action not in ACTIONS:
        raise ReviewError(f"action must be one of {', '.join(ACTIONS)}")
    try:
        kind, ident = ratified.split_ref(ref)
    except ratified.RatifyError as exc:
        raise ReviewError(str(exc)) from exc
    if kind == "ent":
        raise ReviewError("entities are ratified, not resolved: use ratify")
    stamp = now_iso()
    if kind == "note":
        return _resolve_note(owner, action, ident, until, stamp)

    rel = _relation(owner, ident)
    if action == "keep":
        changed = [f"rel:{ident}"]
        ratified.ratify(owner, f"rel:{ident}", note)
        for other in entities.list_relations(owner, entity_id=rel["src"], include_closed=False):
            if other["id"] == ident or other["src"] != rel["src"] or other["rel"] != rel["rel"]:
                continue
            if _value(other) == _value(rel):
                continue
            _retract(other["id"], stamp)
            changed.append(f"rel:{other['id']}")
        return {"action": action, "ref": f"rel:{ident}", "changed": changed,
                "entities": _touched(owner, rel)}
    if action == "archive":
        _retract(ident, stamp)
        ratified.unratify(owner, f"rel:{ident}")
        return {"action": action, "ref": f"rel:{ident}", "changed": [f"rel:{ident}"],
                "entities": _touched(owner, rel)}
    if action == "renew":
        stop = _until_arg(until)
        with db() as conn:
            conn.execute("UPDATE relations SET valid_until = ?, updated_at = ? WHERE id = ?",
                         (stop, stamp, ident))
        return {"action": action, "ref": f"rel:{ident}", "changed": [f"rel:{ident}"],
                "expires_at": stop, "entities": _touched(owner, rel)}
    # update
    text = " ".join(str(value or "").split())
    if not text:
        raise ReviewError("update needs a value")
    dst_id = None
    if rel.get("dst"):
        old_dst = entities.get_entity(rel["dst"]) or {}
        dst_id = entities.upsert_entity(owner, text, type=str(old_dst.get("type") or "other"))["id"]
    new = entities.add_relation(
        owner, rel["src"], rel["rel"], dst_id=dst_id, dst_value="" if dst_id else text,
        valid_from=stamp, evidence=rel.get("evidence") or [], confidence=0.9, method="rule")
    if new["id"] != ident and _relation(owner, ident)["status"] == "active":
        entities._close_relation(ident, stamp, status="superseded", superseded_by=new["id"], now=stamp)
    ratified.ratify(owner, f"rel:{new['id']}", note)
    return {"action": action, "ref": f"rel:{ident}", "changed": [f"rel:{ident}", f"rel:{new['id']}"],
            "new_ref": f"rel:{new['id']}", "entities": _touched(owner, rel, new)}


def _until_arg(until: Optional[str]) -> str:
    if until is None:
        raise ReviewError("renew needs until (an ISO date, or an empty string to clear it)")
    text = str(until).strip()
    if not text:
        return ""
    parsed = parse_iso(text)
    if parsed is None:
        raise ReviewError(f"until is not a date: {text!r}")
    return parsed.isoformat().replace("+00:00", "Z")


def _resolve_note(owner: str, action: str, path: str, until: Optional[str], stamp: str) -> Dict[str, Any]:
    from src.brain import frontmatter as fm
    from src.brain import notes

    ref = f"note:{path}"
    if action == "archive":
        notes.delete_note(owner, path)
        ratified.unratify(owner, ref)
        return {"action": action, "ref": ref, "changed": [ref], "entities": []}
    if action == "renew":
        stop = _until_arg(until)
        current = notes.read_note(owner, path)
        front = current.get("frontmatter") or {}
        key = next((k for k in EXPIRY_KEYS if k in front), "caduca")
        patched = fm.patch_header(current["content"], {key: stop[:10] if stop else None})
        notes.write_note(owner, path, patched)
        return {"action": action, "ref": ref, "changed": [ref], "expires_at": stop, "entities": []}
    raise ReviewError(f"a note supports archive and renew, not {action}")


__all__ = ["ReviewError", "ACTIONS", "EXPIRY_KEYS", "contradictions", "expired", "overview", "resolve"]
