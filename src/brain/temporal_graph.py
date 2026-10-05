"""Bounded graph retrieval with separate valid-time and recorded-time filters."""
from __future__ import annotations

from typing import Any
from pydantic import BaseModel, ConfigDict, Field


class GraphQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    query: str = Field(default="", max_length=400)
    entity_id: str | None = Field(default=None, max_length=64)
    as_of: str | None = Field(default=None, max_length=40)
    known_at: str | None = Field(default=None, max_length=40)
    depth: int = Field(default=1, ge=1, le=3)
    limit: int = Field(default=50, ge=1, le=100)


def retrieve(owner, request: GraphQuery) -> dict[str, Any]:
    from src.brain import entities, relation_history
    from src.brain.db import db, parse_iso, fold
    for stamp in (request.as_of, request.known_at):
        if stamp is not None and parse_iso(stamp) is None:
            raise ValueError("time filters must be ISO dates/times")
    # Current visibility always wins, even in a historical query. No per-entity
    # profile lookup here: graph reads should not issue hundreds of nested reads.
    with db() as conn:
        rows = conn.execute("SELECT id,name,type,aliases FROM entities WHERE owner=? AND hidden=0 AND merged_into=''",
                            (str(owner or ""),)).fetchall()
    visible = {r["id"]: {"id": r["id"], "name": r["name"], "type": r["type"], "aliases": r["aliases"]} for r in rows}
    if request.entity_id:
        roots = [request.entity_id] if request.entity_id in visible else []
    elif request.query:
        term = fold(request.query)
        roots = [key for key, row in visible.items() if term in fold(row["name"] + " " + row["aliases"])]
    else:
        roots = list(visible)
    relations = entities.list_relations(owner, as_of=request.as_of, known_at=request.known_at,
                                       include_closed=request.as_of is not None)
    edges, selected, frontier = [], set(roots[:request.limit]), set(roots[:request.limit])
    seen = set()
    truncated = len(roots) > request.limit
    for _ in range(request.depth):
        following = set()
        for relation in relations:
            src, dst = relation["src"], relation["dst"]
            if relation["id"] in seen or src not in visible or (dst and dst not in visible):
                continue
            if src not in frontier and dst not in frontier:
                continue
            additions = {key for key in (src, dst) if key and key not in selected}
            if len(selected) + len(additions) > request.limit or len(edges) >= 300:
                truncated = True
                continue
            selected.update(additions)
            following.update(additions)
            seen.add(relation["id"])
            edges.append(relation)
        frontier = following
        if not frontier:
            break
    return {"nodes": [{k: v for k, v in row.items() if k != "aliases"} for key, row in visible.items() if key in selected],
        "edges": edges, "as_of": request.as_of, "known_at": request.known_at,
        "recorded_from": relation_history.horizon(owner), "truncated": truncated,
        "entity_labels": "current", "untrusted_content": True}
