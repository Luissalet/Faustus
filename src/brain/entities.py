"""brain/entities.py — typed entities and time-windowed relations.

The learned memory store (``memory_engine``) and the personal one
(``memory.json``) both hold sentences: "Ada works at Cordera Labs" sits
there as TEXT, indistinguishable from any other fact. This module pulls the
same information into a small graph — an ``Ada`` entity, a ``works_at``
relation to a ``Cordera Labs`` entity — with an explicit validity window on
every relation, so "worked at Cordera Labs until March" and "works at
Bluehaven" are both true statements about Ada, at different times, instead
of a contradiction nobody can resolve.

Nothing here replaces the sentence stores: a relation's ``evidence`` is a
list of the very ``source_ref`` strings (``mem:<id>``, ``pmem:<id>``,
``note:<path>``) the facts came from, so every edge in the graph can always
be traced back to the words that produced it.

Three concepts:

* **Entities** — a name, a type (person/project/organization/place/tool/
  concept/event/other), and a set of aliases folded for matching
  (``fold_name``, ``src.brain.db.fold``). ``upsert_entity`` is alias-aware:
  asking for a name that folds equal to an existing entity or one of its
  aliases returns THAT entity instead of creating a duplicate.
* **Relations** — ``(src, rel, dst_or_dst_value)`` with a validity window.
  A small closed set of relations is FUNCTIONAL (a person has exactly one
  current employer, one current home city): asserting a new value for one
  of these automatically closes the old one's window rather than leaving
  two "current" answers open at once.
* **The owner as an entity** — ``self_entity`` gives every owner a stable
  ``person`` entity aliased "yo"/"me" so "I use X" attaches to a graph node
  the same way "Ada uses X" does.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.brain.db import db, dumps, fold, loads, now_iso, parse_iso, register_schema

logger = logging.getLogger(__name__)

TYPES: Tuple[str, ...] = (
    "person", "project", "organization", "place", "tool", "concept", "event", "other",
)

KNOWN_RELATIONS: Tuple[str, ...] = (
    "works_at", "works_on", "uses", "prefers", "lives_in", "located_in",
    "part_of", "member_of", "knows", "owns", "created", "depends_on",
    "is_a", "related_to", "studied_at",
)

# A person has exactly one CURRENT employer, home and location — asserting a
# new one closes the old one's window. `prefers` is deliberately excluded
# (a preference is fuzzy across object categories: "prefers tabs" and
# "prefers coffee" do not compete) and so is `is_a` (a thing can be an
# instance of more than one category at once).
FUNCTIONAL_RELATIONS = frozenset({"works_at", "lives_in", "located_in"})

# Folded aliases that resolve a mention straight to `self_entity(owner)`
# rather than through the general name/alias match.
SELF_WORDS: Tuple[str, ...] = ("yo", "me", "i")

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS entities (
        id                  TEXT PRIMARY KEY,
        owner               TEXT NOT NULL DEFAULT '',
        project             TEXT NOT NULL DEFAULT '',
        name                TEXT NOT NULL DEFAULT '',
        fold_name           TEXT NOT NULL DEFAULT '',
        type                TEXT NOT NULL DEFAULT 'other',
        aliases             TEXT NOT NULL DEFAULT '[]',
        summary             TEXT NOT NULL DEFAULT '',
        summary_sources     TEXT NOT NULL DEFAULT '[]',
        summary_locked      INTEGER NOT NULL DEFAULT 0,
        summary_updated_at  TEXT NOT NULL DEFAULT '',
        facts_hash          TEXT NOT NULL DEFAULT '',
        hidden              INTEGER NOT NULL DEFAULT 0,
        merged_into         TEXT NOT NULL DEFAULT '',
        created_at          TEXT NOT NULL DEFAULT '',
        updated_at          TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_entities_owner ON entities(owner, fold_name)",
    """
    CREATE TABLE IF NOT EXISTS relations (
        id            TEXT PRIMARY KEY,
        owner         TEXT NOT NULL DEFAULT '',
        project       TEXT NOT NULL DEFAULT '',
        src           TEXT NOT NULL DEFAULT '',
        rel           TEXT NOT NULL DEFAULT '',
        dst           TEXT NOT NULL DEFAULT '',
        dst_value     TEXT NOT NULL DEFAULT '',
        valid_from    TEXT NOT NULL DEFAULT '',
        valid_until   TEXT NOT NULL DEFAULT '',
        asserted_at   TEXT NOT NULL DEFAULT '',
        evidence      TEXT NOT NULL DEFAULT '[]',
        confidence    REAL NOT NULL DEFAULT 0.6,
        method        TEXT NOT NULL DEFAULT 'rule',
        status        TEXT NOT NULL DEFAULT 'active',
        superseded_by TEXT NOT NULL DEFAULT '',
        created_at    TEXT NOT NULL DEFAULT '',
        updated_at    TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_relations_owner_src ON relations(owner, src)",
    "CREATE INDEX IF NOT EXISTS idx_relations_owner_dst ON relations(owner, dst)",
    """
    CREATE TABLE IF NOT EXISTS mentions (
        owner       TEXT NOT NULL DEFAULT '',
        entity_id   TEXT NOT NULL DEFAULT '',
        source_ref  TEXT NOT NULL DEFAULT '',
        created_at  TEXT NOT NULL DEFAULT '',
        PRIMARY KEY(entity_id, source_ref)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_mentions_source ON mentions(source_ref)",
)
register_schema("brain_entities", _SCHEMA)


class BrainEntityError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Row <-> dict
# ---------------------------------------------------------------------------


def _row_to_entity(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"], "owner": row["owner"], "project": row["project"],
        "name": row["name"], "fold_name": row["fold_name"], "type": row["type"],
        "aliases": loads(row["aliases"], []), "summary": row["summary"] or "",
        "summary_sources": loads(row["summary_sources"], []),
        "summary_locked": bool(row["summary_locked"]),
        "summary_updated_at": row["summary_updated_at"] or "",
        "facts_hash": row["facts_hash"] or "",
        "hidden": bool(row["hidden"]), "merged_into": row["merged_into"] or "",
        "created_at": row["created_at"] or "", "updated_at": row["updated_at"] or "",
    }


def _row_to_relation(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"], "owner": row["owner"], "project": row["project"],
        "src": row["src"], "rel": row["rel"], "dst": row["dst"] or "",
        "dst_value": row["dst_value"] or "",
        "valid_from": row["valid_from"] or "", "valid_until": row["valid_until"] or "",
        "asserted_at": row["asserted_at"] or "", "evidence": loads(row["evidence"], []),
        "confidence": float(row["confidence"] or 0.0), "method": row["method"] or "rule",
        "status": row["status"] or "active", "superseded_by": row["superseded_by"] or "",
        "created_at": row["created_at"] or "", "updated_at": row["updated_at"] or "",
    }


def _clean_aliases(aliases: Any) -> List[str]:
    out: List[str] = []
    seen = set()
    for alias in (aliases or ()):
        text = " ".join(str(alias or "").split())
        if not text:
            continue
        key = fold(text)
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def _normalize_rel(value: Any) -> str:
    """Fold to a stable key. Free text is allowed (the vocabulary above is
    not enforced), but it must fold the same way every time it is typed."""
    return fold(value).replace(" ", "_")


def _coerce_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    parsed = parse_iso(value)
    return parsed or datetime.now(timezone.utc)


def _norm_dt_arg(value: Any) -> str:
    """A caller-supplied valid_from/valid_until (datetime, ISO string, or
    None) -> a stored ISO string, or ``""`` when nothing usable was given."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if value.tzinfo else \
            value.replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    parsed = parse_iso(value)
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ") if parsed else ""


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------


def _match_entity(conn: sqlite3.Connection, owner: str, fold_name: str,
                  alias_folds: set) -> Optional[sqlite3.Row]:
    """The entity `fold_name`/`alias_folds` resolves to, following a merge
    chain to its final (non-merged) target. None if nothing matches."""
    rows = conn.execute("SELECT * FROM entities WHERE owner = ?", (owner,)).fetchall()
    for row in rows:
        row_folds = {row["fold_name"]} | {fold(a) for a in loads(row["aliases"], [])}
        if fold_name in row_folds or (row_folds & alias_folds):
            target = row
            seen = set()
            while target["merged_into"] and target["id"] not in seen:
                seen.add(target["id"])
                nxt = conn.execute(
                    "SELECT * FROM entities WHERE id = ?", (target["merged_into"],)
                ).fetchone()
                if not nxt:
                    break
                target = nxt
            return target
    return None


def upsert_entity(owner: Any, name: Any, *, type: str = "other",  # noqa: A002
                  aliases: Sequence[str] = (), project: str = "") -> Dict[str, Any]:
    """Alias/fold-aware create-or-find. A `name` (or alias) that folds equal
    to an existing entity's name or alias returns THAT entity — its aliases
    are extended with anything new, never duplicated."""
    owner = str(owner or "")
    name = " ".join(str(name or "").split())
    if not name:
        raise BrainEntityError("entity name must not be empty")
    etype = str(type or "other").strip().lower()
    if etype not in TYPES:
        etype = "other"
    alias_list = _clean_aliases(aliases)
    fold_name = fold(name)
    alias_folds = {fold(a) for a in alias_list}
    now = now_iso()
    with db() as conn:
        existing = _match_entity(conn, owner, fold_name, alias_folds)
        if existing:
            entity_id = str(existing["id"])
            current_aliases = loads(existing["aliases"], [])
            merged = list(current_aliases)
            merged_folds = {fold(a) for a in merged} | {existing["fold_name"]}
            if fold_name != existing["fold_name"] and fold_name not in merged_folds:
                merged.append(name)
                merged_folds.add(fold_name)
            for alias in alias_list:
                if fold(alias) not in merged_folds:
                    merged.append(alias)
                    merged_folds.add(fold(alias))
            if merged != current_aliases:
                conn.execute("UPDATE entities SET aliases = ?, updated_at = ? WHERE id = ?",
                            (dumps(merged), now, entity_id))
            return _row_to_entity(conn.execute(
                "SELECT * FROM entities WHERE id = ?", (entity_id,)).fetchone())

        entity_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO entities (id, owner, project, name, fold_name, type, aliases, "
            "summary, summary_sources, summary_locked, summary_updated_at, facts_hash, "
            "hidden, merged_into, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (entity_id, owner, str(project or ""), name, fold_name, etype,
            dumps(alias_list), "", "[]", 0, "", "", 0, "", now, now),
        )
        return _row_to_entity(conn.execute(
            "SELECT * FROM entities WHERE id = ?", (entity_id,)).fetchone())


def get_entity(entity_id: Any) -> Optional[Dict[str, Any]]:
    with db() as conn:
        row = conn.execute("SELECT * FROM entities WHERE id = ?", (str(entity_id or ""),)).fetchone()
    return _row_to_entity(row) if row else None


_ENTITY_JSON_FIELDS = {"aliases", "summary_sources"}
_ENTITY_BOOL_FIELDS = {"summary_locked", "hidden"}
_ENTITY_SETTABLE = {
    "name", "type", "aliases", "summary", "summary_locked", "hidden",
    "summary_sources", "facts_hash", "project", "merged_into",
}


def update_entity(entity_id: Any, **fields: Any) -> Optional[Dict[str, Any]]:
    """Generic field setter (name/type/aliases/summary/summary_locked/hidden
    plus the wiki-maintained `summary_sources`/`facts_hash`). Unknown keys
    are ignored rather than raising, so a caller can pass a whole partial
    dict through. Returns the updated entity, or None if it does not exist."""
    entity_id = str(entity_id or "")
    entity = get_entity(entity_id)
    if not entity:
        return None
    sets: List[str] = []
    params: List[Any] = []
    for key, value in fields.items():
        if key not in _ENTITY_SETTABLE:
            continue
        if key == "name":
            name = " ".join(str(value or "").split())
            if not name:
                raise BrainEntityError("name must not be empty")
            sets.append("name = ?"); params.append(name)
            sets.append("fold_name = ?"); params.append(fold(name))
        elif key == "type":
            etype = str(value or "other").strip().lower()
            if etype not in TYPES:
                raise BrainEntityError(f"type must be one of {', '.join(TYPES)}")
            sets.append("type = ?"); params.append(etype)
        elif key in _ENTITY_JSON_FIELDS:
            cleaned = _clean_aliases(value) if key == "aliases" else [str(v) for v in (value or [])]
            sets.append(f"{key} = ?"); params.append(dumps(cleaned))
        elif key in _ENTITY_BOOL_FIELDS:
            sets.append(f"{key} = ?"); params.append(1 if value else 0)
        else:
            sets.append(f"{key} = ?"); params.append(str(value or ""))
    if not sets:
        return entity
    if "summary" in fields:
        sets.append("summary_updated_at = ?"); params.append(now_iso())
    sets.append("updated_at = ?"); params.append(now_iso())
    params.append(entity_id)
    with db() as conn:
        conn.execute(f"UPDATE entities SET {', '.join(sets)} WHERE id = ?", params)
    return get_entity(entity_id)


def set_hidden(entity_id: Any, hidden: bool) -> Optional[Dict[str, Any]]:
    return update_entity(entity_id, hidden=bool(hidden))


def list_entities(owner: Any, *, q: str = "", type: str = "", limit: int = 200,  # noqa: A002
                  include_hidden: bool = False) -> List[Dict[str, Any]]:
    owner = str(owner or "")
    where = ["owner = ?", "merged_into = ''"]
    params: List[Any] = [owner]
    if not include_hidden:
        where.append("hidden = 0")
    if type:
        where.append("type = ?"); params.append(str(type))
    sql = f"SELECT * FROM entities WHERE {' AND '.join(where)} ORDER BY updated_at DESC LIMIT ?"
    params.append(max(1, min(2000, int(limit or 200))))
    with db() as conn:
        rows = conn.execute(sql, params).fetchall()
    q_fold = fold(q) if q else ""
    out: List[Dict[str, Any]] = []
    for row in rows:
        entity = _row_to_entity(row)
        if q_fold:
            haystacks = {entity["fold_name"]} | {fold(a) for a in entity["aliases"]}
            if not any(q_fold in h for h in haystacks):
                continue
        entity["mentions"] = sources_for(entity["id"])
        entity["relations"] = list_relations(owner, entity_id=entity["id"], include_closed=False)
        out.append(entity)
    return out


def merge_entities(keep_id: Any, merge_id: Any) -> Dict[str, Any]:
    """Fold `merge_id` into `keep_id`: relations and mentions are
    re-pointed, aliases combined, and `merge_id` is hidden with
    `merged_into` set so any later lookup by its name/alias resolves
    straight through to `keep_id`."""
    keep_id, merge_id = str(keep_id or ""), str(merge_id or "")
    if not keep_id or not merge_id or keep_id == merge_id:
        raise BrainEntityError("merge_entities needs two different entity ids")
    keep = get_entity(keep_id)
    merged = get_entity(merge_id)
    if not keep or not merged:
        raise BrainEntityError("merge_entities: entity not found")
    now = now_iso()
    with db() as conn:
        conn.execute("UPDATE relations SET src = ?, updated_at = ? WHERE src = ?",
                    (keep_id, now, merge_id))
        conn.execute("UPDATE relations SET dst = ?, updated_at = ? WHERE dst = ?",
                    (keep_id, now, merge_id))
        conn.execute("UPDATE relations SET superseded_by = ? WHERE superseded_by = ?",
                    (keep_id, merge_id))
        rows = conn.execute(
            "SELECT source_ref FROM mentions WHERE entity_id = ?", (merge_id,)).fetchall()
        for row in rows:
            conn.execute(
                "INSERT INTO mentions (owner, entity_id, source_ref, created_at) "
                "VALUES (?,?,?,?) ON CONFLICT(entity_id, source_ref) DO NOTHING",
                (merged["owner"], keep_id, row["source_ref"], now),
            )
        conn.execute("DELETE FROM mentions WHERE entity_id = ?", (merge_id,))

        combined = list(keep.get("aliases") or [])
        combined_folds = {fold(a) for a in combined} | {keep["fold_name"]}
        for candidate in [merged["name"], *(merged.get("aliases") or [])]:
            if fold(candidate) not in combined_folds:
                combined.append(candidate)
                combined_folds.add(fold(candidate))
        conn.execute("UPDATE entities SET aliases = ?, updated_at = ? WHERE id = ?",
                    (dumps(combined), now, keep_id))
        conn.execute(
            "UPDATE entities SET merged_into = ?, hidden = 1, updated_at = ? WHERE id = ?",
            (keep_id, now, merge_id),
        )
    return get_entity(keep_id)


def self_entity(owner: Any) -> Dict[str, Any]:
    """The owner's own `person` entity, created on first use. "I use X" /
    "uso X" attach to this node the same way "Ada uses X" attaches to
    Ada's."""
    display = ""
    try:
        from src.settings import get_setting
        display = str(get_setting("owner_display_name", "") or "").strip()
    except Exception:  # noqa: BLE001
        display = ""
    return upsert_entity(owner, display or "Yo", type="person", aliases=SELF_WORDS)


# ---------------------------------------------------------------------------
# Relations
# ---------------------------------------------------------------------------


def _close_relation(relation_id: str, valid_until: str, *, status: str, superseded_by: str = "",
                    now: Optional[str] = None) -> None:
    stamp = now or now_iso()
    with db() as conn:
        conn.execute(
            "UPDATE relations SET valid_until = ?, status = ?, superseded_by = ?, "
            "updated_at = ? WHERE id = ?",
            (valid_until, status, superseded_by, stamp, relation_id),
        )


def _relation_start(relation: Dict[str, Any]) -> Tuple[Optional[datetime], bool]:
    """(start, explicit?) — a relation asserted without a date gets its
    assertion instant as `valid_from`, so "explicit" means they differ."""
    start = parse_iso(relation.get("valid_from"))
    asserted = parse_iso(relation.get("asserted_at"))
    if start is None:
        return asserted, False
    return start, asserted is None or start != asserted


def _supersede_functional(new_rel: Dict[str, Any], current: Dict[str, Any],
                          now: str) -> Optional[str]:
    """Close whichever of `new_rel`/`current` is outdated; returns the id
    that was closed, or None when nothing changed."""
    from src.brain.temporal import supersede_order

    new_start, new_explicit = _relation_start(new_rel)
    cur_start, cur_explicit = _relation_start(current)
    order = supersede_order(new_start, cur_start, new_explicit=new_explicit,
                            old_explicit=cur_explicit)
    if order == "old":
        target, other = current, new_rel
    elif order == "new":
        target, other = new_rel, current
    else:
        return None
    if str(target.get("method") or "rule") == "rule" and str(other.get("method") or "") == "llm":
        return None  # a model's claim never ends a fact read deterministically from the text
    at = str(other.get("valid_from") or "")
    at_dt = parse_iso(at)
    target_start, _ = _relation_start(target)
    target_end = parse_iso(target.get("valid_until"))
    if at_dt is None or (target_start is not None and at_dt < target_start):
        return None
    if target_end is not None and target_end <= at_dt:
        # already ended by then: its window is never extended nor rewritten,
        # it is only marked as followed by the other one
        _close_relation(str(target["id"]), str(target.get("valid_until") or ""),
                        status="superseded", superseded_by=str(other["id"]), now=now)
        return str(target["id"])
    _close_relation(str(target["id"]), at, status="superseded",
                    superseded_by=str(other["id"]), now=now)
    return str(target["id"])


def add_relation(owner: Any, src_id: Any, rel: Any, *, dst_id: Any = None,
                 dst_value: str = "", valid_from: Any = None, valid_until: Any = None,
                 evidence: Sequence[str] = (), confidence: float = 0.6, method: str = "rule",
                 project: str = "") -> Dict[str, Any]:
    """Assert one relation. When `rel` is functional
    (:data:`FUNCTIONAL_RELATIONS`) and `src_id` already has a DIFFERENT
    current value for it, the OUTDATED one of the two is closed
    (`valid_until` set to the other one's `valid_from`, status
    `superseded`, `superseded_by` the other) rather than both left open.

    Chronology decides which one is outdated, not arrival order
    (`temporal.supersede_order`): asserting "works at Cordera Labs since
    2019" after "works at Bluehaven since 2024" closes the 2019 relation at
    2024, never the current one at 2019. A window is never made to end
    before it starts, and one that already ended is never rewritten. When
    the order is unknowable (equal dated starts; a dated start earlier than
    an undated one) both stay open for a human to look at. A model-derived
    relation (method "llm") never closes a rule-derived one."""
    owner = str(owner or "")
    src_id = str(src_id or "")
    rel_key = _normalize_rel(rel)
    if not src_id:
        raise BrainEntityError("add_relation needs src_id")
    if not rel_key:
        raise BrainEntityError("add_relation needs rel")
    dst_id = str(dst_id or "")
    dst_value = "" if dst_id else " ".join(str(dst_value or "").split())[:400]
    if not dst_id and not dst_value:
        raise BrainEntityError("add_relation needs dst_id or dst_value")

    now = now_iso()
    valid_from_iso = _norm_dt_arg(valid_from) or now
    valid_until_iso = _norm_dt_arg(valid_until)

    relation_id = uuid.uuid4().hex
    with db() as conn:
        conn.execute(
            "INSERT INTO relations (id, owner, project, src, rel, dst, dst_value, "
            "valid_from, valid_until, asserted_at, evidence, confidence, method, "
            "status, superseded_by, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (relation_id, owner, str(project or ""), src_id, rel_key, dst_id, dst_value,
            valid_from_iso, valid_until_iso, now, dumps(list(evidence)),
            float(confidence), str(method or "rule"), "active", "", now, now),
        )

    if rel_key in FUNCTIONAL_RELATIONS:
        new_rel = {"id": relation_id, "valid_from": valid_from_iso, "valid_until": valid_until_iso,
                   "asserted_at": now, "method": str(method or "rule")}
        for current in list_relations(owner, entity_id=src_id, include_closed=False):
            if current["id"] == relation_id or current["src"] != src_id or current["rel"] != rel_key:
                continue
            same_dst = (dst_id and current["dst"] == dst_id) or \
                       (not dst_id and current["dst_value"] == dst_value)
            if same_dst:
                continue
            closed = _supersede_functional(new_rel, current, now)
            if closed is not None and closed == relation_id:
                with db() as conn:
                    row = conn.execute("SELECT valid_until FROM relations WHERE id = ?",
                                       (relation_id,)).fetchone()
                new_rel["valid_until"] = (row["valid_until"] if row else "") or ""

    with db() as conn:
        row = conn.execute("SELECT * FROM relations WHERE id = ?", (relation_id,)).fetchone()
    return _row_to_relation(row)


def _relation_window_covers(relation: Dict[str, Any], instant: datetime) -> bool:
    valid_from = parse_iso(relation.get("valid_from"))
    if valid_from and instant < valid_from:
        return False
    valid_until = parse_iso(relation.get("valid_until"))
    if valid_until and instant > valid_until:
        return False
    return True


def list_relations(owner: Any, *, entity_id: Any = None, as_of: Any = None,
                   include_closed: bool = True) -> List[Dict[str, Any]]:
    owner = str(owner or "")
    with db() as conn:
        if entity_id:
            eid = str(entity_id)
            rows = conn.execute(
                "SELECT * FROM relations WHERE owner = ? AND (src = ? OR dst = ?) "
                "ORDER BY created_at",
                (owner, eid, eid),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM relations WHERE owner = ? ORDER BY created_at", (owner,)
            ).fetchall()
    items = [_row_to_relation(row) for row in rows]
    if as_of is not None:
        instant = _coerce_dt(as_of)
        items = [r for r in items if _relation_window_covers(r, instant)]
    elif not include_closed:
        items = [r for r in items if r["status"] == "active"]
    return items


# ---------------------------------------------------------------------------
# Mentions
# ---------------------------------------------------------------------------


def add_mention(owner: Any, entity_id: Any, source_ref: Any) -> None:
    entity_id = str(entity_id or "")
    source_ref = str(source_ref or "")
    if not entity_id or not source_ref:
        return
    with db() as conn:
        conn.execute(
            "INSERT INTO mentions (owner, entity_id, source_ref, created_at) VALUES (?,?,?,?) "
            "ON CONFLICT(entity_id, source_ref) DO NOTHING",
            (str(owner or ""), entity_id, source_ref, now_iso()),
        )


def mentions_for(source_ref: Any) -> List[Dict[str, Any]]:
    with db() as conn:
        rows = conn.execute(
            "SELECT entity_id FROM mentions WHERE source_ref = ?", (str(source_ref or ""),)
        ).fetchall()
    out = []
    for row in rows:
        entity = get_entity(row["entity_id"])
        if entity:
            out.append(entity)
    return out


def sources_for(entity_id: Any) -> List[str]:
    with db() as conn:
        rows = conn.execute(
            "SELECT source_ref FROM mentions WHERE entity_id = ? ORDER BY created_at",
            (str(entity_id or ""),),
        ).fetchall()
    return [row["source_ref"] for row in rows]


# ---------------------------------------------------------------------------
# Mention detection in free text
# ---------------------------------------------------------------------------

# Names shorter than this never match on their own: too easy to collide
# with an ordinary word ("Yo", any single letter). Two-letter aliases like
# "yo"/"me" still pass; the bare "i" from SELF_WORDS deliberately does not
# (self-reference through "I" alone is resolved explicitly by callers that
# know they are looking at a sentence subject, not by this general sweep).
_MIN_MATCH_LEN = 2


def entities_in_text(owner: Any, text: Any, *, limit: int = 8) -> List[Dict[str, Any]]:
    """Known entities (by name or alias) mentioned in `text` — word-boundary,
    fold-insensitive, longest name first."""
    owner = str(owner or "")
    text_fold = fold(text)
    if not text_fold:
        return []
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM entities WHERE owner = ? AND merged_into = ''", (owner,)
        ).fetchall()
    candidates: List[Tuple[str, sqlite3.Row]] = []
    for row in rows:
        for name in [row["name"], *loads(row["aliases"], [])]:
            name = str(name or "").strip()
            if len(name) < _MIN_MATCH_LEN:
                continue
            candidates.append((fold(name), row))
    candidates.sort(key=lambda pair: -len(pair[0]))

    matched: List[str] = []
    seen = set()
    for folded_name, row in candidates:
        if row["id"] in seen or not folded_name:
            continue
        if re.search(rf"\b{re.escape(folded_name)}\b", text_fold):
            seen.add(row["id"])
            matched.append(row["id"])
        if len(matched) >= max(1, int(limit or 8)):
            break
    return [get_entity(eid) for eid in matched]


# ---------------------------------------------------------------------------
# Profile / graph / stats
# ---------------------------------------------------------------------------


def _entity_name(entity_id: str) -> str:
    if not entity_id:
        return ""
    entity = get_entity(entity_id)
    return entity["name"] if entity else ""


def _fact_from_source(source_ref: str) -> Optional[Dict[str, Any]]:
    """Best-effort resolution of a mention's source back to its text and
    validity window. Unknown/unreachable prefixes degrade to a stub rather
    than raising — a broken lookup must cost the fact, not the profile."""
    if source_ref.startswith("mem:"):
        try:
            from src import memory_engine
        except Exception:  # noqa: BLE001
            return None
        item = memory_engine.get_item(source_ref[4:])
        if not item:
            return None
        return {
            "source_ref": source_ref, "text": item.get("text", ""),
            "valid_from": item.get("valid_from") or "",
            "valid_until": item.get("valid_until") or "",
            "created_at": item.get("created_at") or "",
        }
    if source_ref.startswith("pmem:"):
        try:
            from src.memory import MemoryManager
            from src.constants import DATA_DIR
        except Exception:  # noqa: BLE001
            return None
        entry_id = source_ref[5:]
        try:
            for entry in MemoryManager(DATA_DIR).load_all():
                if str(entry.get("id")) == entry_id:
                    return {
                        "source_ref": source_ref, "text": entry.get("text", ""),
                        "valid_from": "", "valid_until": "",
                        "created_at": "",
                    }
        except Exception:  # noqa: BLE001
            return None
        return None
    return {"source_ref": source_ref, "text": "", "valid_from": "", "valid_until": "",
            "created_at": ""}


def _fact_valid_at(fact: Dict[str, Any], instant: datetime) -> bool:
    valid_from = parse_iso(fact.get("valid_from"))
    if valid_from and instant < valid_from:
        return False
    valid_until = parse_iso(fact.get("valid_until"))
    if valid_until and instant > valid_until:
        return False
    return True


def _build_timeline(entity: Dict[str, Any], facts: List[Dict[str, Any]],
                    relations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    if entity.get("created_at"):
        events.append({"at": entity["created_at"], "kind": "created",
                       "text": f"Entity created: {entity.get('name', '')}", "source_ref": ""})
    for fact in facts:
        if fact.get("created_at"):
            events.append({"at": fact["created_at"], "kind": "mention",
                           "text": fact.get("text", ""), "source_ref": fact.get("source_ref", "")})
    for rel in relations:
        label = rel.get("dst_name") or rel.get("dst_value") or ""
        text = f"{rel['rel']} {label}".strip()
        if rel.get("valid_from"):
            events.append({"at": rel["valid_from"], "kind": "valid_from", "text": text,
                           "source_ref": rel.get("id", "")})
        if rel.get("valid_until"):
            events.append({"at": rel["valid_until"], "kind": "valid_until", "text": text,
                           "source_ref": rel.get("id", "")})
    events.sort(key=lambda e: e["at"])
    return events


def profile(entity_id: Any, *, as_of: Any = None) -> Dict[str, Any]:
    entity = get_entity(entity_id)
    if not entity:
        raise BrainEntityError(f"unknown entity {entity_id!r}")
    owner = entity["owner"]
    instant = _coerce_dt(as_of) if as_of is not None else datetime.now(timezone.utc)

    facts: List[Dict[str, Any]] = []
    for source_ref in sources_for(entity["id"]):
        fact = _fact_from_source(source_ref)
        if not fact:
            continue
        fact["valid_now"] = _fact_valid_at(fact, instant)
        facts.append(fact)
    facts.sort(key=lambda f: f.get("created_at") or "")

    all_rels = list_relations(owner, entity_id=entity["id"], include_closed=True)
    relations: List[Dict[str, Any]] = []
    history: List[Dict[str, Any]] = []
    for rel in all_rels:
        enriched = dict(rel)
        enriched["src_name"] = _entity_name(rel["src"])
        enriched["dst_name"] = _entity_name(rel["dst"]) if rel["dst"] else ""
        enriched["valid_at"] = _relation_window_covers(rel, instant)
        relations.append(enriched)
        if rel["status"] != "active":
            history.append(enriched)

    return {
        "entity": entity,
        "facts": facts,
        "relations": relations,
        "history": history,
        "timeline": _build_timeline(entity, facts, relations),
        "summary": entity.get("summary", ""),
        "summary_sources": entity.get("summary_sources", []),
    }


def graph(owner: Any, *, limit: int = 500) -> Dict[str, Any]:
    owner = str(owner or "")
    entities = list_entities(owner, limit=limit, include_hidden=False)
    id_set = {e["id"] for e in entities}
    nodes = [{"id": f"ent:{e['id']}", "label": e["name"], "kind": e["type"], "degree": 0}
            for e in entities]
    degree: Dict[str, int] = {}
    edges: List[Dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    for rel in list_relations(owner, include_closed=True):
        if rel["src"] not in id_set or not rel["dst"] or rel["dst"] not in id_set:
            continue
        edges.append({
            "from": f"ent:{rel['src']}", "to": f"ent:{rel['dst']}", "kind": rel["rel"],
            "valid_now": rel["status"] == "active" and _relation_window_covers(rel, now),
        })
        degree[rel["src"]] = degree.get(rel["src"], 0) + 1
        degree[rel["dst"]] = degree.get(rel["dst"], 0) + 1
    for node in nodes:
        node["degree"] = degree.get(node["id"].split(":", 1)[1], 0)
    return {"nodes": nodes, "edges": edges}


def stats(owner: Any) -> Dict[str, Any]:
    owner = str(owner or "")
    with db() as conn:
        total = conn.execute(
            "SELECT COUNT(*) c FROM entities WHERE owner = ? AND merged_into = '' AND hidden = 0",
            (owner,),
        ).fetchone()["c"]
        hidden = conn.execute(
            "SELECT COUNT(*) c FROM entities WHERE owner = ? AND merged_into = '' AND hidden = 1",
            (owner,),
        ).fetchone()["c"]
        by_type_rows = conn.execute(
            "SELECT type, COUNT(*) c FROM entities WHERE owner = ? AND merged_into = '' "
            "AND hidden = 0 GROUP BY type", (owner,),
        ).fetchall()
        relations_active = conn.execute(
            "SELECT COUNT(*) c FROM relations WHERE owner = ? AND status = 'active'", (owner,)
        ).fetchone()["c"]
        relations_total = conn.execute(
            "SELECT COUNT(*) c FROM relations WHERE owner = ?", (owner,)
        ).fetchone()["c"]
    return {
        "entities": total, "hidden": hidden,
        "by_type": {row["type"]: row["c"] for row in by_type_rows},
        "relations_active": relations_active, "relations_total": relations_total,
    }


__all__ = [
    "TYPES", "KNOWN_RELATIONS", "FUNCTIONAL_RELATIONS", "SELF_WORDS", "BrainEntityError",
    "upsert_entity", "get_entity", "update_entity", "set_hidden", "list_entities",
    "merge_entities", "self_entity", "add_relation", "list_relations", "add_mention",
    "mentions_for", "sources_for", "entities_in_text", "profile", "graph", "stats",
]
