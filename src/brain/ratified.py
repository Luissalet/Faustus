"""brain/ratified.py — what the owner has vouched for.

Facts and notes reach the brain two ways: the owner writes them, or an agent
files them (extraction, the wiki pass, a chat tool). Both look the same once
stored. Ratifying marks an item as *confirmed by the owner*, and a ratified
item wins over an agent-filed one in three places:

* a functional relation (one employer, one home city) that is ratified is never
  closed by a newer, unratified value — both stay open and show up in
  :mod:`src.brain.review` as a contradiction, with the ratified one first;
* the context engine's brain source grades a ratified entity or note as the
  owner's own statement (``human_explicit`` / ``user_instruction``) instead of
  an agent claim, and drops an unratified relation that contradicts a ratified
  one from an entity card;
* every list that shows facts (``/facts``, profiles, reviews) carries the flag.

The mark lives in its own small table keyed by ``(owner, ref)`` so it never
touches the row it describes, survives a re-extraction of that row and can be
removed without side effects. A ``ref`` is the same scheme the rest of the
brain uses:

    rel:<relation_id>   one fact (a relation with its validity window)
    ent:<entity_id>     an entity — its summary and card
    note:<vault_path>   a free or mirrored note
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional

from src.brain.db import db, now_iso, register_schema

logger = logging.getLogger(__name__)

KINDS = ("rel", "ent", "note")

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS brain_ratified (
        owner        TEXT NOT NULL DEFAULT '',
        ref          TEXT NOT NULL DEFAULT '',
        ratified_at  TEXT NOT NULL DEFAULT '',
        note         TEXT NOT NULL DEFAULT '',
        PRIMARY KEY(owner, ref)
    )
    """,
)
register_schema("brain_ratified", _SCHEMA)


class RatifyError(ValueError):
    pass


def split_ref(ref: Any) -> tuple:
    text = str(ref or "").strip()
    kind, _, rest = text.partition(":")
    if kind not in KINDS or not rest:
        raise RatifyError(f"ref must look like rel:<id>, ent:<id> or note:<path>, got {text!r}")
    return kind, rest


def _exists(owner: str, kind: str, ident: str) -> bool:
    if kind == "rel":
        with db() as conn:
            row = conn.execute("SELECT owner FROM relations WHERE id = ?", (ident,)).fetchone()
        return bool(row) and (not row["owner"] or row["owner"] == owner)
    if kind == "ent":
        from src.brain import entities
        entity = entities.get_entity(ident)
        return bool(entity) and (not entity.get("owner") or entity["owner"] == owner)
    from src.brain import notes
    try:
        notes.read_note(owner, ident)
    except (FileNotFoundError, ValueError):
        return False
    return True


def ratify(owner: Any, ref: Any, note: str = "") -> Dict[str, Any]:
    """Mark `ref` as ratified. Idempotent; asking again refreshes the note."""
    owner = str(owner or "")
    kind, ident = split_ref(ref)
    if not _exists(owner, kind, ident):
        raise RatifyError(f"{kind} not found: {ident}")
    full = f"{kind}:{ident}"
    stamp = now_iso()
    with db() as conn:
        conn.execute(
            "INSERT INTO brain_ratified (owner, ref, ratified_at, note) VALUES (?,?,?,?) "
            "ON CONFLICT(owner, ref) DO UPDATE SET note = excluded.note",
            (owner, full, stamp, " ".join(str(note or "").split())[:400]),
        )
        row = conn.execute("SELECT * FROM brain_ratified WHERE owner = ? AND ref = ?",
                           (owner, full)).fetchone()
    return _row(row)


def unratify(owner: Any, ref: Any) -> bool:
    owner = str(owner or "")
    kind, ident = split_ref(ref)
    with db() as conn:
        cur = conn.execute("DELETE FROM brain_ratified WHERE owner = ? AND ref = ?",
                           (owner, f"{kind}:{ident}"))
    return cur.rowcount > 0


def _row(row: Any) -> Dict[str, Any]:
    return {"ref": row["ref"], "ratified_at": row["ratified_at"], "note": row["note"]}


def refs(owner: Any, prefix: str = "") -> Dict[str, Dict[str, Any]]:
    """Every ratified ref of `owner` (optionally one kind: ``"rel:"``)."""
    owner = str(owner or "")
    with db() as conn:
        rows = conn.execute("SELECT * FROM brain_ratified WHERE owner = ? AND ref LIKE ? "
                            "ORDER BY ratified_at DESC", (owner, f"{prefix}%")).fetchall()
    return {r["ref"]: _row(r) for r in rows}


def is_ratified(owner: Any, ref: Any) -> bool:
    try:
        kind, ident = split_ref(ref)
    except RatifyError:
        return False
    with db() as conn:
        row = conn.execute("SELECT 1 FROM brain_ratified WHERE owner = ? AND ref = ?",
                           (str(owner or ""), f"{kind}:{ident}")).fetchone()
    return row is not None


def annotate(owner: Any, rows: Iterable[Dict[str, Any]], kind: str = "rel",
             key: str = "id") -> List[Dict[str, Any]]:
    """Add ``ratified`` (bool) and ``ratified_at`` to every dict in `rows`."""
    items = list(rows)
    marks = refs(owner, f"{kind}:")
    for item in items:
        mark = marks.get(f"{kind}:{item.get(key)}")
        item["ratified"] = mark is not None
        item["ratified_at"] = mark["ratified_at"] if mark else ""
    return items


def move(owner: Any, old_ref: str, new_ref: str) -> None:
    """Carry a mark to a renamed note; never raises."""
    try:
        with db() as conn:
            conn.execute("UPDATE OR REPLACE brain_ratified SET ref = ? WHERE owner = ? AND ref = ?",
                         (new_ref, str(owner or ""), old_ref))
    except Exception:  # noqa: BLE001
        logger.debug("brain.ratified: move failed", exc_info=True)


def forget(owner: Any, ref: Any) -> None:
    """Drop the mark of a deleted item; never raises."""
    try:
        unratify(owner, ref)
    except Exception:  # noqa: BLE001
        logger.debug("brain.ratified: forget failed", exc_info=True)


__all__ = ["RatifyError", "ratify", "unratify", "refs", "is_ratified", "annotate", "forget", "move",
           "split_ref", "KINDS"]
