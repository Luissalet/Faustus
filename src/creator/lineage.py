"""lineage.py — derived_from edges for Creator's media library (WP03).

CONTRATO.md rule 1: no autoridad paralela. ``src.contracts.blob.Relations``
already carries a ``derived_from`` field (a tuple of PARENT occurrence ids) on
every ``ArtifactOccurrence``, and ``ArtifactOccurrence.provenance`` already
carries ``recipe``/``recipe_version``/``recipe_fingerprint``/``inputs_digest``/
``engine``/``engine_job_id``. Together those already say everything WP03's
edge shape asks for — ``{child_occurrence, parent_occurrence, recipe_id,
params_hash, engine_build}`` — so this module reads them; it does not add a
lineage table of its own.

``docs/03_ARQUITECTURA_Y_CONTRATOS.md`` line 33 confirms the shape is
intentional: "Proxies y miniaturas tienen relaciones `derived_from` y receta;
no se transforman en una nueva identidad compartida entre propietarios."

There is no reverse index (a parent does not know its children — only a child
names its parents), so ``descendants()`` and ``rebuild_graph()`` scan one
owner's one project's occurrences, which is the same bounded set
``src.artifact_catalog.recent()`` already reads for the Library list. That is
a real, documented limitation (see the report): a very large project pays an
O(occurrences) scan on every descendants query. Acceptable for the plan's
project-scoped Library; a project-wide reverse index is future work if a
project outgrows this.
"""
from __future__ import annotations

from typing import Any, Dict, List, Set, Tuple

from .errors import CreatorError


class LineageItemNotFound(CreatorError):
    """No such occurrence, or it belongs to someone else — 404 either way,
    per CONTRATO.md rule 3."""


def _edge(child_id: str, parent_id: str, provenance) -> Dict[str, Any]:
    recipe_id = provenance.recipe or ""
    if provenance.recipe_version:
        recipe_id = f"{recipe_id}@{provenance.recipe_version}" if recipe_id else provenance.recipe_version
    return {
        "child_occurrence": child_id,
        "parent_occurrence": parent_id,
        "recipe_id": recipe_id,
        "params_hash": provenance.inputs_digest or provenance.recipe_fingerprint or "",
        "engine_build": provenance.engine_job_id or provenance.engine or "",
    }


def _own(occurrence_id: str, owner: str):
    from src import artifact_identity as identity
    try:
        return identity.for_owner(occurrence_id, owner=owner)
    except (identity.ArtifactNotFound, identity.NotTheOwner) as exc:
        raise LineageItemNotFound(str(exc)) from exc


def direct_parents(owner: str, occurrence_id: str) -> List[Dict[str, Any]]:
    """The edges straight above this occurrence — what it names in its own
    ``relations.derived_from``, none of it inferred."""
    occ = _own(occurrence_id, owner)
    return [_edge(occ.id, parent_id, occ.provenance)
            for parent_id in occ.relations.derived_from]


def ancestors(owner: str, occurrence_id: str, *, max_depth: int = 64) -> List[Dict[str, Any]]:
    """Every edge reachable going up from ``occurrence_id``, breadth-first,
    de-duplicated by (child, parent) so a diamond dependency is walked once
    and cut off at ``max_depth`` hops so a cyclical mistake elsewhere cannot
    hang this call (a citation graph should never cycle, but this reads
    stored data it did not validate on the way in)."""
    edges: List[Dict[str, Any]] = []
    seen_edges: Set[Tuple[str, str]] = set()
    seen_nodes = {occurrence_id}
    frontier = [occurrence_id]
    depth = 0
    while frontier and depth < max_depth:
        next_frontier: List[str] = []
        for node in frontier:
            for edge in direct_parents(owner, node):
                key = (edge["child_occurrence"], edge["parent_occurrence"])
                if key in seen_edges:
                    continue
                seen_edges.add(key)
                edges.append(edge)
                parent = edge["parent_occurrence"]
                if parent not in seen_nodes:
                    seen_nodes.add(parent)
                    next_frontier.append(parent)
        frontier = next_frontier
        depth += 1
    return edges


def rebuild_graph(owner: str, project_id: str) -> Dict[str, Any]:
    """Nodes + edges for one owner's project, read fresh from the occurrence
    table every call. Nothing here is cached, so there is nothing to "rebuild"
    in the sense of repairing drift — the name matches the ficha's asked-for
    surface (``rebuild_graph``), not a stale index this replaces."""
    if not project_id:
        raise ValueError("project_id is required")
    from core.database import SessionLocal
    from src import artifact_catalog, artifact_identity as identity

    with SessionLocal() as db:
        rows = artifact_catalog.recent(db, owner=owner, project_id=project_id, limit=200)
    nodes = []
    edges = []
    for row in rows:
        nodes.append({"occurrence_id": row.id, "kind": row.kind, "label": row.label or ""})
        occ = identity.resolve(row.id)
        if occ is None or not occ.belongs_to(owner):
            continue  # a not-yet-migrated legacy row: no relations to read
        for parent_id in occ.relations.derived_from:
            edges.append(_edge(occ.id, parent_id, occ.provenance))
    return {"project_id": project_id, "nodes": nodes, "edges": edges}


def descendants(owner: str, project_id: str, occurrence_id: str) -> List[Dict[str, Any]]:
    """Every edge reachable going down from ``occurrence_id`` inside one
    project — found by building the project's graph once and walking it
    forward, since no row stores its own children."""
    if not project_id:
        raise ValueError("project_id is required")
    graph = rebuild_graph(owner, project_id)
    by_parent: Dict[str, List[Dict[str, Any]]] = {}
    for edge in graph["edges"]:
        by_parent.setdefault(edge["parent_occurrence"], []).append(edge)

    out: List[Dict[str, Any]] = []
    seen_edges: Set[Tuple[str, str]] = set()
    seen_nodes = {occurrence_id}
    frontier = [occurrence_id]
    while frontier:
        next_frontier: List[str] = []
        for node in frontier:
            for edge in by_parent.get(node, ()):
                key = (edge["child_occurrence"], edge["parent_occurrence"])
                if key in seen_edges:
                    continue
                seen_edges.add(key)
                out.append(edge)
                child = edge["child_occurrence"]
                if child not in seen_nodes:
                    seen_nodes.add(child)
                    next_frontier.append(child)
        frontier = next_frontier
    return out


__all__ = ["LineageItemNotFound", "direct_parents", "ancestors",
           "descendants", "rebuild_graph"]
