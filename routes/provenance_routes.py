"""Provenance graph API — /api/provenance/* (FAUSTUS).

The audit view over ``src/provenance_graph.py``: a 2D graph whose every edge
was read from a record something already stored — a dependency the user
declared, an evidence span, a checkpoint diff, a citation that resolves to a
page, a literally verified text overlap. Nothing a model asserted.

Four reads:

* ``GET /api/provenance/graph`` — the whole (bounded) graph plus ``stats``;
* ``GET /api/provenance/node/{node_id}/explain`` — the ordered evidence chain
  for one node: "why does the agent believe this";
* ``GET /api/provenance/node/{node_id}/neighbors`` — the multi-hop subgraph and
  the ``impact`` set ("what breaks if I touch this");
* ``GET /api/provenance/orphans`` — the nodes nothing points at, grouped by
  kind, plus the verified near-duplicate pairs.

Admin-only, like the rest of the brain: the graph names real workspace paths,
chat sessions and the standing rules the agent follows, so reading it is a
privileged view of the machine.

Robot mode (``?robot=1`` / ``?format=toon``, src/robot_envelope.py) is on all
four, with the LEAN projections at the bottom of this module rather than in
``src/robot_projection.py`` — they are this feature's own rows and belong with
it. Nodes and edges are exactly what TOON's tabular form was built for: one
fixed, all-scalar column tuple per row, so the encoder writes one header
instead of a key set per line. ``meta`` is dropped (its useful scalars are
folded into the row), and a call WITHOUT those query parameters answers exactly
as it always did, with everything in it.

Node ids contain ``:`` and, for files, ``/`` (``file:src/app.py``), so the two
per-node routes take the id with a ``:path`` converter.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from core.middleware import require_admin
from src import robot_envelope as robot
from src.auth_helpers import effective_user

logger = logging.getLogger(__name__)

MAX_LIMIT = 20_000


def _owner(request: Request) -> str:
    try:
        return str(effective_user(request) or "")
    except Exception:  # noqa: BLE001 - attribution must not 500 the route
        return ""


def _resolve_project(project_id: Optional[str], owner: str) -> Optional[Dict[str, Any]]:
    """The project record for ``?project=``, or None.

    A project id that does not resolve is not an error: the graph is simply
    built without the objectives source, which is the module's whole posture.
    """
    project_id = str(project_id or "").strip()
    if not project_id:
        return None
    try:
        from services.projects import get_store
        return get_store().get(project_id, owner or None)
    except Exception as exc:  # noqa: BLE001 - an optional source
        logger.debug("provenance: project %s unresolved (%s)", project_id, exc)
        return None


def _kinds(raw: Optional[str]) -> List[str]:
    return [part.strip().lower() for part in str(raw or "").split(",") if part.strip()]


def _missing(graph_mod, node_id: str) -> str:
    """The 404 detail — which tells the caller when the graph is simply off,
    rather than letting a disabled feature read as an empty workspace."""
    if not graph_mod.enabled():
        return ("the provenance graph is turned off in Settings → Agent & automation, "
                "so it has no nodes")
    return f"no node '{node_id}' in the provenance graph"


def setup_provenance_routes() -> APIRouter:
    router = APIRouter(prefix="/api/provenance", tags=["provenance"])

    def _build(request: Request, project: Optional[str], workspace: Optional[str],
               limit: Optional[int]) -> Dict[str, Any]:
        from src import provenance_graph as graph_mod
        if not graph_mod.enabled():
            # The toggle does something real: nothing is read from disk at all,
            # and every read answers on an empty graph with ``enabled: false``
            # so the page can say "turned off" instead of "nothing found".
            return {"nodes": [], "edges": [], "truncated": False,
                    "sources": {"settings": {"available": False, "count": 0,
                                             "note": "the provenance graph is turned off in "
                                                     "Settings → Agent & automation"}}}
        owner = _owner(request)
        # A bare ``?workspace=`` is enough to read that folder's objectives:
        # services/objectives.py only ever looks at a project's ``workspace``.
        record = _resolve_project(project, owner)
        if record is None and workspace:
            record = {"workspace": str(workspace)}
        try:
            budget = int(limit) if limit else graph_mod.max_nodes()
        except (TypeError, ValueError):
            budget = graph_mod.max_nodes()
        return graph_mod.build(
            owner or None,
            project=record,
            workspace=(str(workspace) if workspace else None),
            limit_nodes=max(1, min(MAX_LIMIT, budget)),
        )

    @router.get("/graph")
    async def get_graph(
        request: Request,
        project: Optional[str] = None,
        workspace: Optional[str] = None,
        kinds: Optional[str] = None,
        limit: Optional[int] = None,
        _admin: None = Depends(require_admin),
    ) -> Dict[str, Any]:
        """The declared-edge graph, bounded by the node budget.

        ``kinds`` is a comma-separated list of node kinds (objective, memory,
        chat, file, checkpoint, expert, corpus); an edge whose endpoint was
        filtered out goes with it.
        """
        from src import provenance_graph as graph_mod

        def payload() -> Dict[str, Any]:
            graph = graph_mod.filter_kinds(_build(request, project, workspace, limit),
                                           _kinds(kinds))
            return {
                "status": "success",
                "nodes": graph.get("nodes") or [],
                "edges": graph.get("edges") or [],
                "sources": graph.get("sources") or {},
                "truncated": bool(graph.get("truncated")),
                "stats": graph_mod.stats(graph),
                "node_kinds": list(graph_mod.NODE_KINDS),
                "edge_kinds": list(graph_mod.EDGE_KINDS),
                "enabled": graph_mod.enabled(),
                "limit": max(1, min(MAX_LIMIT, int(limit) if limit else graph_mod.max_nodes())),
            }
        if robot.wants(request):
            return await robot.reply(request, lambda: _lean_graph(payload()))
        return payload()

    @router.get("/node/{node_id:path}/explain")
    async def explain_node(
        node_id: str,
        request: Request,
        project: Optional[str] = None,
        workspace: Optional[str] = None,
        limit: Optional[int] = None,
        _admin: None = Depends(require_admin),
    ) -> Dict[str, Any]:
        """Why this node is here: the ordered chain of declared records."""
        from src import provenance_graph as graph_mod

        def payload() -> Dict[str, Any]:
            graph = _build(request, project, workspace, limit)
            answer = graph_mod.explain(graph, node_id)
            if answer.get("missing"):
                raise HTTPException(status_code=404, detail=_missing(graph_mod, node_id))
            return {
                "status": "success",
                "node": answer.get("node"),
                "steps": answer.get("steps") or [],
                "summary": answer.get("summary") or "",
                "enabled": graph_mod.enabled(),
            }
        if robot.wants(request):
            return await robot.reply(request, lambda: _lean_explain(payload()))
        return payload()

    @router.get("/node/{node_id:path}/neighbors")
    async def node_neighbors(
        node_id: str,
        request: Request,
        hops: int = 2,
        project: Optional[str] = None,
        workspace: Optional[str] = None,
        limit: Optional[int] = None,
        _admin: None = Depends(require_admin),
    ) -> Dict[str, Any]:
        """The subgraph within ``hops`` steps, plus what breaks if you touch it."""
        from src import provenance_graph as graph_mod

        def payload() -> Dict[str, Any]:
            graph = _build(request, project, workspace, limit)
            sub = graph_mod.neighbors(graph, node_id, hops)
            if sub.get("missing"):
                raise HTTPException(status_code=404,
                                    detail=f"no node '{node_id}' in the provenance graph")
            reachable = graph_mod.impact(graph, node_id)
            by_id = {n["id"]: n for n in (graph.get("nodes") or []) if isinstance(n, dict)}
            return {
                "status": "success",
                "root": sub.get("root"),
                "hops": sub.get("hops"),
                "nodes": sub.get("nodes") or [],
                "edges": sub.get("edges") or [],
                "impact": [by_id[i] for i in reachable if i in by_id],
                "impact_ids": reachable,
                "enabled": graph_mod.enabled(),
            }
        if robot.wants(request):
            return await robot.reply(request, lambda: _lean_neighbors(payload()))
        return payload()

    @router.get("/orphans")
    async def get_orphans(
        request: Request,
        project: Optional[str] = None,
        workspace: Optional[str] = None,
        limit: Optional[int] = None,
        _admin: None = Depends(require_admin),
    ) -> Dict[str, Any]:
        """Nodes nothing points at, and the verified near-duplicate pairs."""
        from src import provenance_graph as graph_mod

        def payload() -> Dict[str, Any]:
            graph = _build(request, project, workspace, limit)
            loose = graph_mod.orphans(graph)
            by_id = {n["id"]: n for n in (graph.get("nodes") or []) if isinstance(n, dict)}
            duplicates = []
            for edge in graph.get("edges") or []:
                if not isinstance(edge, dict) or edge.get("kind") != "duplicate_of":
                    continue
                duplicates.append({
                    "a": edge.get("from"),
                    "b": edge.get("to"),
                    "a_label": (by_id.get(edge.get("from")) or {}).get("label", ""),
                    "b_label": (by_id.get(edge.get("to")) or {}).get("label", ""),
                    "ratio": edge.get("confidence"),
                    "why": edge.get("why"),
                    "spans": ((edge.get("meta") or {}).get("spans") or []),
                })
            return {
                "status": "success",
                "orphans": loose.get("by_kind") or {},
                "orphan_ids": loose.get("ids") or [],
                "count": loose.get("count") or 0,
                "duplicates": duplicates,
                "stats": graph_mod.stats(graph),
                "enabled": graph_mod.enabled(),
            }
        if robot.wants(request):
            return await robot.reply(request, lambda: _lean_orphans(payload()))
        return payload()

    return router


# ---------------------------------------------------------------------------
# Robot-mode projections (the pattern of src/robot_projection.py, kept here
# because these rows are this feature's own)
# ---------------------------------------------------------------------------

_NODE_COLUMNS = ("id", "kind", "label", "detail", "status", "path", "score")
_EDGE_COLUMNS = ("from", "to", "kind", "confidence", "trust", "why")
_STEP_COLUMNS = ("order", "hop", "direction", "kind", "from", "to", "confidence",
                 "trust", "why")
_DUPLICATE_COLUMNS = ("a", "b", "ratio", "a_label", "b_label", "why")


def _cell(value: Any, limit: int = 300) -> str:
    if value is None:
        return ""
    if value is True:
        return "true"
    if value is False:
        return "false"
    try:
        out = " ".join(str(value).split())
    except Exception:  # noqa: BLE001
        return ""
    return out if len(out) <= limit else out[: limit - 1].rstrip() + "…"


def _node_row(node: Any) -> Dict[str, Any]:
    """One node as a flat, all-scalar row.

    ``meta`` is the reason a node is not tabular — every node kind carries a
    different key set in it — so it is dropped and the three scalars a
    coordinator acts on (``status`` for an objective or a job, ``path`` for a
    file, ``effective_score`` for a memory item) are folded in as columns that
    are always present.
    """
    row = node if isinstance(node, dict) else {}
    meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
    score = meta.get("effective_score")
    return {
        "id": _cell(row.get("id"), 200),
        "kind": _cell(row.get("kind"), 40),
        "label": _cell(row.get("label"), 200),
        "detail": _cell(row.get("detail"), 200),
        "status": _cell(meta.get("status"), 40),
        "path": _cell(meta.get("path") or meta.get("source"), 300),
        "score": score if isinstance(score, (int, float)) and not isinstance(score, bool) else None,
    }


def _edge_row(edge: Any) -> Dict[str, Any]:
    row = edge if isinstance(edge, dict) else {}
    confidence = row.get("confidence")
    return {
        "from": _cell(row.get("from"), 200),
        "to": _cell(row.get("to"), 200),
        "kind": _cell(row.get("kind"), 40),
        "confidence": confidence if isinstance(confidence, (int, float))
        and not isinstance(confidence, bool) else None,
        "trust": _cell(row.get("trust"), 20),
        "why": _cell(row.get("why"), 300),
    }


def _step_row(step: Any) -> Dict[str, Any]:
    row = step if isinstance(step, dict) else {}
    confidence = row.get("confidence")
    return {
        "order": row.get("order") if isinstance(row.get("order"), int) else None,
        "hop": row.get("hop") if isinstance(row.get("hop"), int) else None,
        "direction": _cell(row.get("direction"), 20),
        "kind": _cell(row.get("kind"), 40),
        "from": _cell(row.get("from"), 200),
        "to": _cell(row.get("to"), 200),
        "confidence": confidence if isinstance(confidence, (int, float))
        and not isinstance(confidence, bool) else None,
        "trust": _cell(row.get("trust"), 20),
        "why": _cell(row.get("why"), 300),
    }


def _seq(value: Any) -> List[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _guard(fn):
    """A projection that meets a payload it did not expect answers with that
    payload untouched — robot mode may lose the compaction, never the read."""
    def guarded(payload: Any) -> Any:
        if not isinstance(payload, dict):
            return payload
        try:
            return fn(payload)
        except Exception:  # noqa: BLE001 - a view may never break a response
            return payload
    guarded.__name__ = fn.__name__
    guarded.__doc__ = fn.__doc__
    return guarded


@_guard
def _lean_graph(payload: Dict[str, Any]) -> Dict[str, Any]:
    """The graph as two tables plus the source report.

    Dropped: every node's ``meta`` object, the ``node_kinds`` / ``edge_kinds``
    enum tables the UI paints its filter chips from, and ``status: "success"``
    (the envelope's ``ok`` says that).
    """
    stats = payload.get("stats") if isinstance(payload.get("stats"), dict) else {}
    sources = payload.get("sources") if isinstance(payload.get("sources"), dict) else {}
    return {
        "nodes": [_node_row(n) for n in _seq(payload.get("nodes"))],
        "edges": [_edge_row(e) for e in _seq(payload.get("edges"))],
        "sources": [{"source": _cell(name, 40),
                     "available": bool((row or {}).get("available")),
                     "count": (row or {}).get("count"),
                     "note": _cell((row or {}).get("note"), 300)}
                    for name, row in sorted(sources.items())],
        "truncated": bool(payload.get("truncated")),
        "nodes_total": stats.get("nodes"),
        "edges_total": stats.get("edges"),
        "orphans_total": stats.get("orphans"),
    }


@_guard
def _lean_explain(payload: Dict[str, Any]) -> Dict[str, Any]:
    """The evidence chain as one table of steps, each carrying its ``why``."""
    node = payload.get("node") if isinstance(payload.get("node"), dict) else {}
    return {
        "node": _cell(node.get("id"), 200),
        "kind": _cell(node.get("kind"), 40),
        "label": _cell(node.get("label"), 200),
        "summary": _cell(payload.get("summary"), 300),
        "steps": [_step_row(s) for s in _seq(payload.get("steps"))],
    }


@_guard
def _lean_neighbors(payload: Dict[str, Any]) -> Dict[str, Any]:
    """The subgraph as two tables; ``impact`` collapses to its id list, which
    is already a scalar array and repeats no key."""
    return {
        "root": _cell(payload.get("root"), 200),
        "hops": payload.get("hops") if isinstance(payload.get("hops"), int) else None,
        "nodes": [_node_row(n) for n in _seq(payload.get("nodes"))],
        "edges": [_edge_row(e) for e in _seq(payload.get("edges"))],
        "impact": [_cell(i, 200) for i in _seq(payload.get("impact_ids"))],
    }


@_guard
def _lean_orphans(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Orphans as one table (the by-kind grouping is a per-kind key set, which
    never tabularises — the ``kind`` column carries it instead) and the
    duplicate pairs as another, without their span offsets."""
    grouped = payload.get("orphans") if isinstance(payload.get("orphans"), dict) else {}
    rows: List[Dict[str, Any]] = []
    for kind in sorted(grouped):
        for node in _seq(grouped.get(kind)):
            rows.append(_node_row(node))
    duplicates = []
    for pair in _seq(payload.get("duplicates")):
        row = pair if isinstance(pair, dict) else {}
        ratio = row.get("ratio")
        duplicates.append({
            "a": _cell(row.get("a"), 200),
            "b": _cell(row.get("b"), 200),
            "ratio": ratio if isinstance(ratio, (int, float))
            and not isinstance(ratio, bool) else None,
            "a_label": _cell(row.get("a_label"), 200),
            "b_label": _cell(row.get("b_label"), 200),
            "why": _cell(row.get("why"), 300),
        })
    return {"orphans": rows, "count": payload.get("count"), "duplicates": duplicates}
