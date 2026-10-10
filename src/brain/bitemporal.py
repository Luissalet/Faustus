"""brain/bitemporal.py - one read surface for the brain's two time axes.

The storage already is bitemporal (no second store):

* **valid time** - ``relations.valid_from`` / ``valid_until`` (when the fact
  held in the world; empty = unknown / still open);
* **recorded time** - ``relations.asserted_at`` plus the append-only
  ``relation_versions`` log written by SQL triggers (``relation_history``).

A new value for a FUNCTIONAL relation (``entities.FUNCTIONAL_RELATIONS``)
closes the old one (``valid_until`` set, ``status = superseded``,
``superseded_by``) instead of overwriting it. This module adds the missing
public read: :func:`facts`, rows with explicit ``recorded_at`` and
``superseded_at`` (the recorded instant the fact was retired, taken from the
version log; empty while it is still current, and empty when a ``known_at``
view predates the retirement - Faustus did not know yet).

World time is a half-open interval ``[valid_from, valid_until)``: a fact closed
exactly at ``as_of`` is already over (the old and new value never both hold).
Views: current (default, same as ``entities.list_relations``), ``as_of``
(world time T) and ``known_at`` (what Faustus believed on date D); both may
be combined. Pure reads, owner-scoped.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

_RETIRED = ("superseded", "retracted")


def _stamp(value: Any) -> str:
    from datetime import timezone
    from src.brain.db import parse_iso
    instant = parse_iso(value)
    if instant is None:
        raise ValueError("time filters must be ISO dates/times")
    return instant.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _superseded_at(owner: str, ids: List[str], horizon: Optional[str]) -> Dict[str, str]:
    """relation id -> recorded_at when its current retired stretch began.

    A restore (status back to active) clears it, so a relation that was
    retracted and then restored reports empty, and one retired again reports
    the later instant; history before the horizon is kept in the log."""
    if not ids:
        return {}
    import json
    from src.brain.db import db
    out: Dict[str, str] = {}
    with db() as conn:
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            marks = ",".join("?" * len(chunk))
            sql = (f"SELECT relation_id, recorded_at, payload FROM relation_versions "
                   f"WHERE owner=? AND relation_id IN ({marks}) ORDER BY version")
            for row in conn.execute(sql, (owner, *chunk)).fetchall():
                rid = row["relation_id"]
                if horizon is not None and row["recorded_at"] > horizon:
                    continue
                try:
                    status = json.loads(row["payload"]).get("status")
                except Exception:  # noqa: BLE001
                    continue
                if status in _RETIRED:
                    # keep the start of the CURRENT retired stretch only
                    out.setdefault(rid, row["recorded_at"])
                else:
                    # restored (revalidate -> undo): not retired any more
                    out.pop(rid, None)
    return out


def facts(owner: Any, *, entity_id: Any = None, rel: Any = None, as_of: Any = None,
          known_at: Any = None, include_closed: Optional[bool] = None,
          limit: int = 200) -> Dict[str, Any]:
    """Bitemporal relation view.

    With no ``as_of``/``known_at`` and ``include_closed`` unset this is the
    current view (active relations only). Setting ``as_of`` keeps relations
    whose valid window covered that instant (closed ones included);
    ``known_at`` reconstructs the log as of that recorded instant. Each row
    gains ``recorded_at`` (= ``asserted_at``) and ``superseded_at``.
    """
    from src.brain import entities
    if as_of is not None:
        _stamp(as_of)
    horizon = _stamp(known_at) if known_at is not None else None
    if include_closed is None:
        include_closed = as_of is not None
    items = entities.list_relations(owner, entity_id=entity_id or None, as_of=as_of,
                                    known_at=known_at, include_closed=include_closed)
    if as_of is not None:
        # World-time interval is half-open [valid_from, valid_until): a fact
        # closed exactly at T (works_at A -> B on T) is B at T, not A and B.
        from src.brain.db import parse_iso
        instant = parse_iso(as_of) or entities._coerce_dt(as_of)
        items = [r for r in items
                 if not (parse_iso(r.get("valid_until")) and parse_iso(r.get("valid_until")) <= instant)]
    if rel:
        items = [r for r in items if r.get("rel") == str(rel).strip().lower()]
    limit = max(1, min(int(limit or 200), 500))
    truncated = len(items) > limit
    items = items[:limit]
    sup = _superseded_at(str(owner or ""), [r["id"] for r in items], horizon)
    rows = []
    for r in items:
        row = dict(r)
        row["recorded_at"] = r.get("asserted_at") or r.get("created_at") or ""
        row["superseded_at"] = sup.get(r["id"], "")
        rows.append(row)
    mode = "current"
    if as_of is not None and known_at is not None:
        mode = "as_of+known_at"
    elif as_of is not None:
        mode = "as_of"
    elif known_at is not None:
        mode = "known_at"
    return {"mode": mode, "as_of": as_of, "known_at": known_at, "count": len(rows),
            "truncated": truncated, "facts": rows,
            "functional_relations": sorted(entities.FUNCTIONAL_RELATIONS)}


__all__ = ["facts"]