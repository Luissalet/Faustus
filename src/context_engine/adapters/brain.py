"""
context_engine/adapters/brain.py — the second brain as a retrieval source.

``src/brain/*`` is a store the user (and the agent, through the ``brain``
tool) builds by hand: a markdown vault of free notes, and typed entities with
facts and relations extracted from what has already been said. This adapter
is what lets a compiled context packet draw on that store the same way it
draws on ``memory_engine`` — without duplicating a single row of it.

Two kinds of candidate, one section (``retrieved_memory``, same as
``adapters/memory.py``):

* an **entity card** for every entity the request's own text mentions by
  name or alias (``entities.entities_in_text``) — its type, summary, current
  relations and most recent facts, so a question about someone the vault
  already tracks does not have to be re-explained;
* the top **free-note hits** for the request's query (``notes.search``), for
  the notes nobody taught the entity extractor to read.

Both are gated the same way ``adapters/memory.py`` gates its two stores: off
entirely under ``allow_personal_memory=False`` (an entity profile or a note
is exactly the kind of thing incognito must not surface), and additionally
off unless ``brain_enabled`` AND ``brain_context_source`` are both on — the
second is this source's own opt-out, independent of the vault sync and
extraction passes it reads from.

Honesty about what these candidates are: an entity card is composed from
facts `src/brain/extract.py` already grounded in literal source text, so it
is graded ``agent_assertion`` / ``agent_claim`` — an agent's reading, not the
user's own words. A note's body is read from disk as it stands right now, so
it is graded ``observed`` / ``observed_state``, the same floor
``adapters/memory.py`` gives a freshly-read row.

``source_ref`` schemes:

    ent:<entity_id>      an entity; reopen with ``entities.get_entity``
    note:<vault_path>     a free note; reopen with ``notes.read_note``
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..candidates import RetrievalRequest, ThreadedSource, make_candidate
from ..contracts import ContextCandidate

logger = logging.getLogger(__name__)

#: How many recent facts make it into an entity card's body. A card is meant
#: to orient a turn, not replace `brain_entity`/the entity page.
MAX_FACTS = 3
MAX_RELATIONS = 5


def _brain_setting(key: str, default: bool) -> bool:
    try:
        from src.settings import get_setting

        return bool(get_setting(key, default))
    except Exception:  # noqa: BLE001 - an unreadable setting means off
        return False


class BrainSource(ThreadedSource):
    """``src/brain/*`` — vault notes and typed entities, as retrieval."""

    source_id = "brain"
    sections = ("retrieved_memory",)
    handles = ("ent:", "note:")

    def available(self) -> bool:
        try:
            from src.brain import entities, notes  # noqa: F401
        except Exception as exc:  # noqa: BLE001
            logger.debug("brain adapter unavailable: %s", exc)
            return False
        return True

    def _gate(self, req: RetrievalRequest) -> str:
        if not req.policy.allow_personal_memory:
            return "policy.allow_personal_memory is false"
        if not req.wants("retrieved_memory"):
            return "retrieved_memory not requested"
        if not _brain_setting("brain_enabled", True):
            return "brain_enabled is false"
        if not _brain_setting("brain_context_source", True):
            return "brain_context_source is false"
        return ""

    # ── search ─────────────────────────────────────────────────────────────

    def _search(self, req: RetrievalRequest) -> Sequence[ContextCandidate]:
        owner = str(req.owner or "")
        query = str(req.query or "").strip()
        if not owner or not query:
            # No query text to find a mention or a note hit in, and no
            # standing/mandatory section this source fills — unlike
            # memory_engine's procedural rules, nothing here is "always true".
            return ()

        limit = req.top()
        out: List[ContextCandidate] = []
        if req.allows("lexical") or req.allows("exact"):
            out.extend(self._entity_candidates(owner, query, req))
            out.extend(self._note_candidates(owner, query, req))
        return tuple(out[:limit])

    def _entity_candidates(self, owner: str, query: str,
                           req: RetrievalRequest) -> List[ContextCandidate]:
        from src.brain import entities

        try:
            mentioned = entities.entities_in_text(owner, query, limit=8)
        except Exception as exc:  # noqa: BLE001 - one bad lookup costs itself
            logger.debug("brain adapter: entities_in_text failed: %s", exc)
            return []
        out: List[ContextCandidate] = []
        for entity in mentioned:
            candidate = self._entity_candidate(owner, entity, req)
            if candidate is not None:
                out.append(candidate)
        return out

    def _entity_candidate(self, owner: str, entity: Dict[str, Any],
                          req: RetrievalRequest) -> Optional[ContextCandidate]:
        from src.brain import entities

        entity_id = str(entity.get("id") or "")
        if not entity_id:
            return None
        try:
            profile = entities.profile(entity_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug("brain adapter: profile(%s) failed: %s", entity_id, exc)
            return None
        return make_candidate(
            source_type="memory",
            source_ref=f"ent:{entity_id}",
            section="retrieved_memory",
            title=f"{entity.get('name') or entity_id} ({entity.get('type') or 'other'})",
            body=_entity_body(entity, profile),
            lanes=("exact",),
            scores={"mention": 1.0},
            trust_class="agent_assertion",
            authority="agent_claim",
            source_revision=str(entity.get("updated_at") or ""),
            observed_at=entity.get("updated_at") or entity.get("created_at") or "",
            owner=str(entity.get("owner") or owner),
            project_id=req.project_id,
            meta={"entity_type": str(entity.get("type") or ""),
                 "aliases": list(entity.get("aliases") or [])},
        )

    def _note_candidates(self, owner: str, query: str,
                         req: RetrievalRequest) -> List[ContextCandidate]:
        from src.brain import notes

        try:
            hits = notes.search(owner, query, limit=8)
        except Exception as exc:  # noqa: BLE001
            logger.debug("brain adapter: notes.search failed: %s", exc)
            return []
        out: List[ContextCandidate] = []
        for hit in hits:
            candidate = self._note_candidate(owner, hit, req)
            if candidate is not None:
                out.append(candidate)
        return out

    def _note_candidate(self, owner: str, hit: Dict[str, Any],
                        req: RetrievalRequest) -> Optional[ContextCandidate]:
        path = str(hit.get("path") or "")
        if not path:
            return None
        score = float(hit.get("score") or 0.0)
        return make_candidate(
            source_type="memory",
            source_ref=f"note:{path}",
            section="retrieved_memory",
            title=str(hit.get("title") or path),
            body=str(hit.get("snippet") or ""),
            lanes=("lexical",),
            scores={"relevance": score},
            trust_class="observed",
            authority="observed_state",
            owner=owner,
            project_id=req.project_id,
            meta={"kind": str(hit.get("kind") or ""), "path": path},
        )

    # ── fetch ──────────────────────────────────────────────────────────────

    def _fetch(self, source_ref: str,
              req: RetrievalRequest) -> Optional[ContextCandidate]:
        ref = str(source_ref or "")
        owner = str(req.owner or "")
        if ref.startswith("ent:"):
            from src.brain import entities

            entity = entities.get_entity(ref[4:])
            if not entity:
                return None
            stored_owner = str(entity.get("owner") or "")
            if stored_owner and owner and stored_owner != owner:
                logger.debug("refusing %s - owned by another actor", ref)
                return None
            return self._entity_candidate(owner, entity, req)
        if ref.startswith("note:"):
            from src.brain import notes

            path = ref[5:]
            try:
                note = notes.read_note(owner, path)
            except (FileNotFoundError, ValueError):
                return None
            return make_candidate(
                source_type="memory",
                source_ref=ref,
                section="retrieved_memory",
                title=str(note.get("title") or path),
                body=str(note.get("user_zone") or note.get("content") or ""),
                lanes=("exact",),
                scores={},
                trust_class="observed",
                authority="observed_state",
                owner=owner,
                project_id=req.project_id,
                meta={"kind": str(note.get("kind") or ""), "path": path},
            )
        return None


def _entity_body(entity: Dict[str, Any], profile: Dict[str, Any]) -> str:
    """Name, summary, current relations and the most recent facts — enough
    to orient a turn without duplicating the full entity page."""
    lines: List[str] = []
    summary = str(profile.get("summary") or "").strip()
    if summary:
        lines.append(summary)

    active = [r for r in (profile.get("relations") or [])
             if r.get("status") == "active"][:MAX_RELATIONS]
    if active:
        rel_text = "; ".join(
            f"{r.get('rel')} {r.get('dst_name') or r.get('dst') or ''}".strip()
            for r in active
        )
        lines.append(f"Relations: {rel_text}")

    facts = [f for f in (profile.get("facts") or []) if f.get("text")][-MAX_FACTS:]
    if facts:
        lines.append("Facts: " + " | ".join(str(f["text"]) for f in facts))

    if not lines:
        aliases = ", ".join(entity.get("aliases") or [])
        lines.append(f"Known as: {aliases}" if aliases else "No facts recorded yet.")
    return "\n".join(lines)


__all__ = ["BrainSource"]
