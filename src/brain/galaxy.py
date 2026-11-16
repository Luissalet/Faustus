"""brain/galaxy.py — the brain as a navigable cloud of points.

Projects, sessions, agents, outputs and brain entities as five point clouds in
one 3D space, with the links between them (a session belongs to a project and
may run as an agent; an output comes out of a session or a project; entities
are tied by their relations). The layout is computed here, deterministically,
so every client — the Studio WebGL view, an MCP caller — sees the same galaxy:

* projects sit on a sphere of radius 1.0 and are the anchors;
* agents sit on an inner sphere (0.45);
* a session orbits its project, an output orbits its session (or its project);
  anything without a parent lies on an outer shell (1.55 for sessions, 1.75 for
  outputs);
* entities fill a shell of their own (2.1), ordered by name.

A source that cannot be read costs its own cloud, never the answer: the reason
comes back in ``warnings``.
"""

from __future__ import annotations

import hashlib
import logging
import math
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

KINDS = ("project", "agent", "session", "output", "entity")
DEFAULT_LIMIT = 300
MAX_LIMIT = 1000

_SIZE = {"project": 7.0, "agent": 5.5, "session": 3.2, "output": 2.4, "entity": 3.0}
_GOLDEN = math.pi * (3.0 - math.sqrt(5.0))


def _fibonacci(index: int, total: int, radius: float) -> Tuple[float, float, float]:
    total = max(total, 1)
    y = 1.0 - 2.0 * (index + 0.5) / total
    ring = math.sqrt(max(0.0, 1.0 - y * y))
    angle = index * _GOLDEN
    return radius * math.cos(angle) * ring, radius * y, radius * math.sin(angle) * ring


def _jitter(key: str, spread: float) -> Tuple[float, float, float]:
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    vec = [(digest[i] / 255.0) * 2.0 - 1.0 for i in range(3)]
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    scale = spread * (0.35 + 0.65 * digest[3] / 255.0)
    return tuple(v / norm * scale for v in vec)  # type: ignore[return-value]


def _near(anchor: Tuple[float, float, float], key: str, spread: float) -> Tuple[float, float, float]:
    dx, dy, dz = _jitter(key, spread)
    return anchor[0] + dx, anchor[1] + dy, anchor[2] + dz


def _round(point: Tuple[float, float, float]) -> List[float]:
    return [round(point[0], 4), round(point[1], 4), round(point[2], 4)]


def _projects(owner: str) -> List[Dict[str, Any]]:
    from services.projects import get_store

    return [p for p in get_store().list(owner) if isinstance(p, dict) and p.get("id")]


def _sessions(owner: str, limit: int) -> List[Dict[str, Any]]:
    from core.database import Session as DbSession, SessionLocal

    with SessionLocal() as db:
        query = db.query(DbSession).filter(DbSession.owner == owner)
        query = query.filter((DbSession.archived == False) | (DbSession.archived.is_(None)))  # noqa: E712
        rows = query.order_by(DbSession.last_message_at.desc().nullslast()).limit(limit).all()
        return [{
            "id": r.id, "name": r.name or r.id, "project_id": r.project_id or "",
            "crew_member_id": r.crew_member_id or "", "mode": r.mode or "",
            "messages": int(r.message_count or 0),
            "at": str(r.last_message_at or r.created_at or ""),
        } for r in rows]


def _agents(owner: str, limit: int) -> List[Dict[str, Any]]:
    from core.database import CrewMember, SessionLocal

    with SessionLocal() as db:
        rows = (db.query(CrewMember).filter(CrewMember.owner == owner)
                .order_by(CrewMember.sort_order).limit(limit).all())
        return [{"id": r.id, "name": r.name or r.id, "model": r.model or ""} for r in rows]


def _outputs(owner: str, limit: int) -> List[Dict[str, Any]]:
    from core.database import SessionLocal
    from src import artifact_catalog

    with SessionLocal() as db:
        rows = artifact_catalog.recent(db, owner=owner, limit=min(limit, 200))
        return [{
            "id": r.id, "name": r.label or r.filename or r.id, "kind": r.kind or "",
            "project_id": r.project_id or "", "session_id": r.session_id or "",
            "at": str(r.created_at or ""),
        } for r in rows]


def _entities(owner: str, limit: int) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    from src.brain import entities, ratified

    marks = ratified.refs(owner, "ent:")
    graph = entities.graph(owner, limit=limit)
    by_id = {n["id"]: n for n in graph["nodes"]}
    items = [{"id": nid.split(":", 1)[1], "name": n["label"], "type": n["kind"],
              "degree": n["degree"], "ratified": nid in marks}
             for nid, n in by_id.items()]
    return items, graph["edges"]


def build(owner: Any, *, limit: int = DEFAULT_LIMIT, kinds: Optional[List[str]] = None) -> Dict[str, Any]:
    """The galaxy of `owner`: ``{nodes, links, counts, warnings, radius}``."""
    owner = str(owner or "")
    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    wanted = [k for k in (kinds or KINDS) if k in KINDS] or list(KINDS)
    warnings: List[str] = []

    def load(kind: str, fn, *args):
        if kind not in wanted:
            return [] if kind != "entity" else ([], [])
        try:
            return fn(*args)
        except Exception as exc:  # noqa: BLE001 - one unreadable source costs its own cloud
            logger.debug("brain.galaxy: %s unavailable: %s", kind, exc, exc_info=True)
            warnings.append(f"{kind}: {exc}")
            return [] if kind != "entity" else ([], [])

    projects = load("project", _projects, owner)
    agents = load("agent", _agents, owner, limit)
    sessions = load("session", _sessions, owner, limit)
    outputs = load("output", _outputs, owner, limit)
    entity_items, entity_edges = load("entity", _entities, owner, limit)

    nodes: List[Dict[str, Any]] = []
    links: List[List[str]] = []
    pos: Dict[str, Tuple[float, float, float]] = {}

    def add(kind: str, ident: str, label: str, point: Tuple[float, float, float], **extra: Any) -> str:
        node_id = f"{kind}:{ident}"
        pos[node_id] = point
        nodes.append({"id": node_id, "kind": kind, "label": str(label)[:120], "pos": _round(point),
                      "size": extra.pop("size", _SIZE[kind]), **extra})
        return node_id

    for i, p in enumerate(projects):
        add("project", p["id"], p.get("name") or p["id"], _fibonacci(i, len(projects), 1.0),
            workspace=str(p.get("workspace") or ""), folder=str(p.get("folder") or ""))
    for i, a in enumerate(agents):
        add("agent", a["id"], a["name"], _fibonacci(i, len(agents), 0.45), model=a["model"])

    orphans = [s for s in sessions if f"project:{s['project_id']}" not in pos]
    orphan_index = {s["id"]: i for i, s in enumerate(orphans)}
    for s in sessions:
        anchor = pos.get(f"project:{s['project_id']}")
        point = (_near(anchor, f"session:{s['id']}", 0.24) if anchor
                 else _fibonacci(orphan_index[s["id"]], len(orphans), 1.55))
        node = add("session", s["id"], s["name"], point, messages=s["messages"], at=s["at"],
                   mode=s["mode"], size=_SIZE["session"] + min(2.0, math.log1p(s["messages"]) / 3.0))
        if anchor:
            links.append([node, f"project:{s['project_id']}"])
        if s["crew_member_id"] and f"agent:{s['crew_member_id']}" in pos:
            links.append([node, f"agent:{s['crew_member_id']}"])

    lone = [o for o in outputs if f"session:{o['session_id']}" not in pos
            and f"project:{o['project_id']}" not in pos]
    lone_index = {o["id"]: i for i, o in enumerate(lone)}
    for o in outputs:
        parent = (f"session:{o['session_id']}" if f"session:{o['session_id']}" in pos
                  else f"project:{o['project_id']}" if f"project:{o['project_id']}" in pos else "")
        point = (_near(pos[parent], f"output:{o['id']}", 0.09) if parent
                 else _fibonacci(lone_index[o["id"]], len(lone), 1.75))
        node = add("output", o["id"], o["name"], point, artifact_kind=o["kind"], at=o["at"])
        if parent:
            links.append([node, parent])

    ordered = sorted(entity_items, key=lambda e: e["name"].lower())
    for i, e in enumerate(ordered):
        add("entity", e["id"], e["name"], _fibonacci(i, len(ordered), 2.1), entity_type=e["type"],
            ratified=e["ratified"], size=_SIZE["entity"] + min(3.0, e["degree"] * 0.4),
            ref=f"ent:{e['id']}")
    for edge in entity_edges:
        a, b = edge["from"].replace("ent:", "entity:", 1), edge["to"].replace("ent:", "entity:", 1)
        if a in pos and b in pos:
            links.append([a, b])

    counts = {k: sum(1 for n in nodes if n["kind"] == k) for k in KINDS}
    return {"nodes": nodes, "links": links, "counts": counts, "warnings": warnings,
            "radius": 2.2 if counts["entity"] else 1.8, "kinds": list(KINDS)}


__all__ = ["KINDS", "build"]
