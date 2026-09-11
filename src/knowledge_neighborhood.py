"""src/knowledge_neighborhood.py — the "why does this matter" view of one
file or requirement (CMP-04).

Faustus already has, in three unconnected places, every fact this module
answers with: what a piece of code must satisfy (``src/requirements/``),
which decision put it there (``src/project_board.py``'s issues), what proves
it still works (``src/requirements/evidence.py``'s coverage matrix) and how
it connects to the rest of the code (``src/context_engine/code_index.py``'s
symbol graph). None of that is duplicated here — this module is a single
read path across the four, typed the same way a code-graph edge already is:

    requirement -> decision(issue) -> symbol -> test -> run

Every edge carries a ``relation`` (never invented, always one of the three
below) and a ``stale: bool`` that is never left implicit:

  ``declared``  — a human or agent recorded this link on purpose (a
                  requirement's ``implements``/``tests``/``issue`` link).
  ``located``   — found mechanically, by the code index or a text search,
                  never asserted as intentional.
  ``verified``  — an ``evidences`` link resolves against the requirement's
                  CURRENT revision. This is the ONLY relation this module
                  will ever call ``verified`` — the same rule
                  ``evidence.py``'s own docstring states, restated here
                  because a second definition of "verified" invented in this
                  module would be exactly the bug CMP-04 exists to prevent.

The whole-module limit, and the reason the decisive test looks the way it
does: a stale ``evidences`` link (the requirement changed since it was
verified) NEVER reports ``relation="verified"`` as if nothing happened —
it reports the edge with ``stale=True`` and a ``why`` that says so in plain
words ("evidencia caducada"), and the requirement's own ``verified`` flag
mirrors ``evidence.matrix()`` exactly, which already treats a stale
``evidences`` link as not-linked for that purpose. Nothing here recomputes
that judgement differently.

No LLM call. Every failure (a missing project, an unindexed workspace, a
board or code-index outage) degrades to an emptier neighborhood, never an
exception the caller has to guard against by hand -- the same posture
``src/requirements/context.py`` and ``code_index.py`` already take.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set, Tuple

from src import project_board
from src.requirements import evidence as req_evidence
from src.requirements import store as req_store

logger = logging.getLogger(__name__)

#: Depth is a small, bounded fan-out knob, not a general graph query --
#: unclamped it would let one request walk the whole code index.
MAX_DEPTH = 3

#: Rows kept per neighborhood. A file with thousands of callers has already
#: made its point in the first few hundred -- see `code_engine.wiring`'s own
#: `MAX_REPORT_ROWS` for the same reasoning applied to a different report.
MAX_NODES = 400
MAX_EDGES = 800

_TEST_HINTS = ("test_", "/tests/", "_test.py", "/test/")

#: Human-legible reasons for a `resolve_link_state` staleness meta, so the
#: neighborhood never repeats the machine `reason` code verbatim to a person.
_STALE_WHY = {
    "content_changed": "el fichero cambió desde que se enlazó",
    "symbol_not_found": "el símbolo ya no está en el fichero",
    "target_missing": "el objetivo del enlace ya no existe",
    "path_outside_workspace": "el objetivo queda fuera del workspace",
    "requirement_changed_since_verification":
        "evidencia caducada: el requisito cambió desde que se verificó",
}


def _looks_like_test(path: str) -> bool:
    low = str(path or "").lower()
    return any(hint in low for hint in _TEST_HINTS)


def _node(nodes: Dict[str, Dict[str, Any]], node_id: str, *, type: str, label: str,
          ref: str = "", why: str = "", stale: bool = False, **extra: Any) -> Dict[str, Any]:
    """Insert-or-merge a node. Merging (never overwriting) means a symbol
    reached both as a requirement's declared target and as a code-index
    neighbor keeps both provenances instead of the second one silently
    winning."""
    row = nodes.get(node_id)
    if row is None:
        row = {"id": node_id, "type": type, "label": label, "ref": ref,
               "why": why, "stale": bool(stale)}
        row.update(extra)
        nodes[node_id] = row
    else:
        if stale:
            row["stale"] = True
        if why and why not in row.get("why", ""):
            row["why"] = (row.get("why") or "") + (" · " if row.get("why") else "") + why
    return row


def _edge(edges: List[Dict[str, Any]], seen: Set[Tuple[str, str, str]], *,
          src: str, dst: str, relation: str, kind: str, stale: bool = False,
          why: str = "") -> None:
    key = (src, dst, kind)
    if key in seen:
        return
    seen.add(key)
    if len(edges) >= MAX_EDGES:
        return
    edges.append({"src": src, "dst": dst, "relation": relation, "kind": kind,
                   "stale": bool(stale), "why": why})


def _requirement_keys_for_path(project_id: str, path: str) -> List[str]:
    """Requirement keys with a declared `implements`/`tests` link whose
    target starts with `path` -- the same "this file's requirement" match
    `requirements/context.py::_linked_keys_for_files` uses, kept here as its
    own small pass because that helper is private to its module and this one
    needs the matching links too, not just the keys."""
    wanted = str(path or "").strip()
    if not wanted:
        return []
    out: List[str] = []
    for key in req_store.all_requirement_keys(project_id):
        for link in req_store.list_links(project_id, key):
            if link["kind"] not in ("implements", "tests"):
                continue
            target_path = link["target"].split("::")[0].split("@")[0]
            if target_path == wanted or target_path.startswith(wanted.rstrip("/") + "/"):
                if key not in out:
                    out.append(key)
                break
    return out


def _add_issue_node(nodes: Dict[str, Dict[str, Any]], issue_id: str, *, why: str) -> Optional[str]:
    node_id = f"decision:{issue_id}"
    issue = None
    try:
        issue = project_board.get(issue_id)
    except Exception:  # noqa: BLE001 - a board outage degrades, it never raises
        logger.debug("knowledge_neighborhood: could not read issue %s", issue_id, exc_info=True)
    if issue is None:
        _node(nodes, node_id, type="decision", label=issue_id, ref=issue_id,
              why="issue ya no existe" if why is None else why, stale=True)
        return node_id
    _node(nodes, node_id, type="decision", label=f"{issue['id']}: {issue['title']}",
          ref=issue["id"], why=why, status=issue["status"], priority=issue["priority"])
    return node_id


def _add_requirement_edges(project_id: str, key: str, workspace: str, *,
                            nodes: Dict[str, Dict[str, Any]], edges: List[Dict[str, Any]],
                            edge_seen: Set[Tuple[str, str, str]]) -> Dict[str, Any]:
    """Everything a coverage matrix already knows about `key`, translated
    into typed nodes/edges. Never recomputes verified/stale -- reads them
    straight off `evidence.matrix()`."""
    matrix = req_evidence.matrix(project_id, key, workspace=workspace, persist=False)
    requirement = req_store.get(project_id, key) or {}
    req_node_id = f"requirement:{key}"
    _node(nodes, req_node_id, type="requirement", label=f"{key}: {requirement.get('title', '')}",
          ref=key, why=requirement.get("text") or requirement.get("title") or "",
          stale=bool(matrix.get("stale")), status=requirement.get("status", ""),
          verified=bool(matrix.get("verified")), implemented=bool(matrix.get("implemented")),
          tested=bool(matrix.get("tested")), current_revision=requirement.get("current_revision"))

    for link in matrix.get("links") or ():
        kind = link.get("kind")
        target = str(link.get("target") or "")
        live_state = link.get("live_state")
        live_meta = link.get("live_meta") or {}
        is_stale = live_state == "stale"
        reason = str(live_meta.get("reason") or "")
        why = _STALE_WHY.get(reason, reason) if is_stale else ""

        if kind == "issue":
            dst = _add_issue_node(nodes, target, why=f"declarado por {key}")
            if dst:
                _edge(edges, edge_seen, src=req_node_id, dst=dst, relation="declared",
                      kind="issue", stale=is_stale, why=why)
            continue

        if kind in ("implements", "tests"):
            path = target.split("::")[0].split("@")[0]
            node_type = "test" if kind == "tests" or _looks_like_test(path) else "symbol"
            dst = f"{node_type}:{target}"
            _node(nodes, dst, type=node_type, label=target, ref=target,
                  why=f"enlazado por {key} ({kind})", stale=is_stale)
            relation = "declared"
            _edge(edges, edge_seen, src=req_node_id, dst=dst, relation=relation,
                  kind=kind, stale=is_stale, why=why)
            continue

        if kind == "evidences":
            run_node = f"run:{target}"
            _node(nodes, run_node, type="run", label=target, ref=target,
                  why=f"evidencia registrada por {key}", stale=is_stale)
            _edge(edges, edge_seen, src=req_node_id, dst=run_node, relation="verified",
                  kind="evidences", stale=is_stale, why=why)

    return matrix


def _add_code_index_neighbors(project_id: str, path: str, workspace: str, *, depth: int,
                               nodes: Dict[str, Dict[str, Any]], edges: List[Dict[str, Any]],
                               edge_seen: Set[Tuple[str, str, str]]) -> None:
    """Structural neighbors from the code graph (`code_index.neighbors`),
    added as `located` -- mechanically found, never claimed as declared.
    Import kept local: `context_engine.code_index` pulls sqlite/ast machinery
    this module's callers should not pay for on every unrelated read."""
    try:
        from src.context_engine import code_index
    except Exception:  # noqa: BLE001 - the code index is optional infrastructure
        logger.debug("knowledge_neighborhood: code_index unavailable", exc_info=True)
        return
    try:
        symbols = code_index.symbols_in(path, workspace=workspace, project_id=project_id)
    except Exception:  # noqa: BLE001
        logger.debug("knowledge_neighborhood: symbols_in(%s) failed", path, exc_info=True)
        return
    for sym in symbols:
        sym_type = "test" if _looks_like_test(sym.path) else "symbol"
        sym_node = f"{sym_type}:{sym.id}"
        _node(nodes, sym_node, type=sym_type, label=sym.qualname or sym.path, ref=sym.source_ref(),
              why=sym.summary or sym.signature or "definido en este fichero",
              language=sym.language, kind=sym.kind)
        if len(nodes) >= MAX_NODES:
            return
        try:
            hops = code_index.neighbors(sym.id, hops=depth)
        except Exception:  # noqa: BLE001
            logger.debug("knowledge_neighborhood: neighbors(%s) failed", sym.id, exc_info=True)
            continue
        for hop in hops:
            other_id = str(hop.get("symbol_id") or "")
            if not other_id:
                continue
            resolved = bool(hop.get("resolved"))
            other_path = str(hop.get("path") or "")
            other_type = "test" if _looks_like_test(other_path) else "symbol"
            other_node = f"{other_type}:{other_id}"
            other_label = str(hop.get("qualname") or other_id)
            _node(nodes, other_node, type=other_type, label=other_label,
                  ref=(f"symbol:{other_path}#L{hop.get('start_line')}-L{hop.get('end_line')}"
                       if resolved else other_id),
                  why="" if resolved else "referencia externa, no indexada")
            direction = str(hop.get("direction") or "out")
            src, dst = (sym_node, other_node) if direction == "out" else (other_node, sym_node)
            _edge(edges, edge_seen, src=src, dst=dst, relation="located",
                  kind=str(hop.get("edge_kind") or ""), stale=False,
                  why=f"certeza {hop.get('certainty')}" if hop.get("certainty") else "")
            if len(nodes) >= MAX_NODES or len(edges) >= MAX_EDGES:
                return


def _add_mentioning_decisions(project_id: str, needle: str, *, req_node_id: str,
                               nodes: Dict[str, Dict[str, Any]], edges: List[Dict[str, Any]],
                               edge_seen: Set[Tuple[str, str, str]]) -> None:
    """Issues that mention `needle` (a path or a requirement key) in their
    title/body but have no explicit link -- `located`, a text match, never
    presented as a `declared` decision the way a real link is."""
    needle = str(needle or "").strip()
    if not needle:
        return
    try:
        issues, _cursor = project_board.list_issues(project_id, q=needle, limit=20)
    except Exception:  # noqa: BLE001
        logger.debug("knowledge_neighborhood: board search for %r failed", needle, exc_info=True)
        return
    for compact in issues:
        node_id = f"decision:{compact['id']}"
        _node(nodes, node_id, type="decision", label=f"{compact['id']}: {compact['title']}",
              ref=compact["id"], why=f"menciona «{needle}»", status=compact["status"],
              priority=compact["priority"])
        _edge(edges, edge_seen, src=req_node_id, dst=node_id, relation="located",
              kind="mentions", stale=False, why=f"búsqueda de texto: «{needle}»")


def neighborhood(project_id: str, owner: str, *, path: Optional[str] = None,
                  req_key: Optional[str] = None, issue_key: Optional[str] = None,
                  depth: int = 1) -> Dict[str, Any]:
    """The typed neighborhood of one file or requirement.

    At least one of `path`, `req_key`, `issue_key` should be given; with none
    the result is an empty, well-formed neighborhood rather than "every
    requirement in the project" -- the same "no free-text search stands in
    for scope" limit `requirements/context.py::for_task` already holds to.

    `owner` is accepted (and unused beyond documenting the call site) for the
    same reason `context_engine.wiring.build_request` takes it explicitly:
    the caller must always be able to say whose turn this is, even though
    this module's own stores (`requirements`, `project_board`, `code_index`)
    are scoped by `project_id` alone today.
    """
    project_id = str(project_id or "").strip()
    path = str(path or "").strip().replace("\\", "/") or None
    req_key = str(req_key or "").strip().upper() or None
    issue_key = str(issue_key or "").strip() or None
    try:
        depth = max(1, min(MAX_DEPTH, int(depth or 1)))
    except (TypeError, ValueError):
        depth = 1

    nodes: Dict[str, Dict[str, Any]] = {}
    edges: List[Dict[str, Any]] = []
    edge_seen: Set[Tuple[str, str, str]] = set()
    unknown: List[str] = []

    workspace = ""
    try:
        from services.projects import get_store as get_project_store

        project = get_project_store().get(project_id, owner)
        workspace = str((project or {}).get("workspace") or "")
    except Exception:  # noqa: BLE001 - workspace-less is a degraded, not a failed, read
        logger.debug("knowledge_neighborhood: could not resolve workspace for %s", project_id,
                     exc_info=True)

    req_keys: List[str] = []
    if req_key:
        if req_store.get(project_id, req_key) is None:
            unknown.append(req_key)
        else:
            req_keys.append(req_key)
    if path:
        for key in _requirement_keys_for_path(project_id, path):
            if key not in req_keys:
                req_keys.append(key)

    for key in req_keys:
        if len(nodes) >= MAX_NODES:
            break
        try:
            _add_requirement_edges(project_id, key, workspace, nodes=nodes, edges=edges,
                                    edge_seen=edge_seen)
        except req_store.NotFoundError:
            if key not in unknown:
                unknown.append(key)
        except Exception:  # noqa: BLE001 - one requirement's failure must not blank the rest
            logger.debug("knowledge_neighborhood: requirement %s failed", key, exc_info=True)

    if issue_key:
        dst = _add_issue_node(nodes, issue_key, why="solicitado explícitamente")
        for key in req_keys:
            _edge(edges, edge_seen, src=f"requirement:{key}", dst=dst, relation="declared",
                  kind="issue", stale=False, why="")

    if path:
        try:
            _add_code_index_neighbors(project_id, path, workspace, depth=depth, nodes=nodes,
                                       edges=edges, edge_seen=edge_seen)
        except Exception:  # noqa: BLE001
            logger.debug("knowledge_neighborhood: code index pass failed for %s", path,
                         exc_info=True)
        seed = f"requirement:{req_keys[0]}" if req_keys else f"path:{path}"
        if seed not in nodes and not req_keys:
            _node(nodes, seed, type="path", label=path, ref=path, why="fichero consultado")
        try:
            _add_mentioning_decisions(project_id, path, req_node_id=seed, nodes=nodes,
                                       edges=edges, edge_seen=edge_seen)
        except Exception:  # noqa: BLE001
            logger.debug("knowledge_neighborhood: mention search failed for %s", path,
                         exc_info=True)

    stale_refs = sorted({row["ref"] for row in nodes.values() if row.get("stale") and row.get("ref")})
    return {
        "project_id": project_id,
        "path": path,
        "req_key": req_key,
        "issue_key": issue_key,
        "depth": depth,
        "nodes": list(nodes.values())[:MAX_NODES],
        "edges": edges[:MAX_EDGES],
        "unknown": unknown,
        "stale_refs": stale_refs,
    }


__all__ = ["neighborhood", "MAX_DEPTH", "MAX_NODES", "MAX_EDGES"]
