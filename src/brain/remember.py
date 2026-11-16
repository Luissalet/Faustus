"""brain/remember.py — file an entity and its facts in one call.

The write path shared by ``POST /api/brain/entities`` and the ``brain_remember``
MCP tool, so an agent outside Faustus can put a fact into the brain exactly the
way the Studio does: create-or-find an entity by name or alias, cite sources for
it and assert relations from it. Idempotent: the same call twice leaves one
entity, one mention per source and one edge per relation.
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence

from src.brain import entities


def remember(owner: Any, name: Any, *, type: str = "other",  # noqa: A002
             aliases: Sequence[Any] = (), summary: str = "", mentions: Sequence[Any] = (),
             relations: Sequence[Any] = (), confidence: float = 0.9) -> Dict[str, Any]:
    """Returns ``{"entity", "created", "relations", "touched"}`` (`touched` =
    entity ids whose vault pages should be re-mirrored). Raises
    :class:`entities.BrainEntityError` (a ``ValueError``) for a bad name,
    relation or date."""
    owner = str(owner or "")
    clean = " ".join(str(name or "").split())
    if not clean:
        raise entities.BrainEntityError("name is required")
    if not isinstance(relations, (list, tuple)) or not isinstance(mentions, (list, tuple)):
        raise entities.BrainEntityError("relations and mentions must be lists")

    known = entities.list_entities(owner, q=clean, limit=200, include_hidden=True)
    before = {e["id"] for e in known}
    entity = entities.upsert_entity(owner, clean, type=str(type or "other"),
                                    aliases=[str(a) for a in (aliases or ()) if str(a).strip()])
    created = entity["id"] not in before and entity.get("created_at") == entity.get("updated_at")
    text = str(summary or "").strip()
    if text and not entity.get("summary_locked") and text != (entity.get("summary") or ""):
        entity = entities.update_entity(entity["id"], summary=text) or entity
    for ref in mentions:
        entities.add_mention(owner, entity["id"], str(ref or "").strip())

    touched: List[str] = [entity["id"]]
    asserted: List[Dict[str, Any]] = []
    for item in relations:
        if not isinstance(item, dict):
            raise entities.BrainEntityError("each relation must be an object")
        rel = entities.canonical_relation(item.get("rel")) or str(item.get("rel") or "")
        dst_id = str(item.get("to_id") or "")
        if dst_id:
            target = entities.get_entity(dst_id)
            if not target or (target.get("owner") or "") != owner:
                raise entities.BrainEntityError("relation target not found")
        elif str(item.get("to") or "").strip():
            target = entities.upsert_entity(owner, item["to"], type=str(item.get("to_type") or "other"))
            dst_id = target["id"]
        if dst_id and dst_id == entity["id"]:
            continue
        evidence = [str(r) for r in (item.get("evidence") or mentions) if str(r or "").strip()]
        asserted.append(entities.add_relation(
            owner, entity["id"], rel, dst_id=dst_id or None, dst_value=str(item.get("value") or ""),
            valid_from=item.get("valid_from") or None, valid_until=item.get("valid_until") or None,
            evidence=evidence, confidence=confidence, method="rule"))
        if dst_id:
            for ref in evidence:
                entities.add_mention(owner, dst_id, ref)
            touched.append(dst_id)
    fresh = entities.get_entity(entity["id"]) or entity
    return {"entity": fresh, "created": created, "relations": asserted, "touched": touched}


__all__ = ["remember"]
