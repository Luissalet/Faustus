"""agent_tools/project_concepts_tools.py — `concepts_*` tool executors.

Six thin executors over `src.project_concepts`, same shape as
`requirement_tools.py`: parse a permissive JSON-object payload, resolve the
project the same way (`ctx["project_id"]` via `services.projects`, falling
back to `ctx["workspace"]`), call straight into the store.

    concepts_understand  read   semantic search over this project's concept
                                 graph (concepts + one-hop neighbours)
    concept_get          read   one concept's full detail (edges, children)
    concepts_roots       read   root concepts (no parent), with child counts
    concept_upsert       write  create or update a concept the agent just
                                 learned about the project
    concept_link         write  attach a typed edge between two concepts
    concept_remove       write  soft-delete a concept

No `concept_unlink` tool is exposed separately -- `concept_link` covers the
common "the agent is teaching the graph" path; removing a stale edge is a
UI/route action, not something the agent needs mid-turn as often as
`req_link`'s counterpart in requirement_tools.py.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from src import project_concepts as pc

logger = logging.getLogger(__name__)


def _args(content: Any) -> Dict[str, Any]:
    raw = (content or "").strip() if isinstance(content, str) else content
    if isinstance(raw, dict):
        return dict(raw)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _project_id(ctx: dict) -> Optional[str]:
    pid = str((ctx or {}).get("project_id") or "").strip()
    return pid or None


def _workspace(ctx: dict) -> str:
    project_id = _project_id(ctx)
    if project_id:
        try:
            from services.projects import get_store
            project = get_store().get(project_id, str((ctx or {}).get("owner") or "") or None)
            ws = (project or {}).get("workspace") or ""
            if ws:
                return ws
        except Exception:
            logger.debug("project_concepts_tools._workspace: project lookup failed", exc_info=True)
    return str((ctx or {}).get("workspace") or "")


def _store_for(ctx: dict) -> Optional[pc.Store]:
    project_id = _project_id(ctx)
    workspace = _workspace(ctx)
    if not project_id and not workspace:
        return None
    key = pc.resolve_project_key(project_id=project_id, workspace=workspace)
    return pc.Store(key)


def _no_project(tool: str) -> Dict[str, Any]:
    return {
        "error": f"{tool}: no project or workspace bound to this chat -- concepts belong to a project. "
                 "Open a project or workspace for this chat first.",
        "exit_code": 1, "error_class": "project_concepts.no_project",
    }


def _pc_error(tool: str, exc: pc.ProjectConceptsError) -> Dict[str, Any]:
    return {"error": f"{tool}: {exc}", "exit_code": 1, "error_class": exc.error_class}


class ConceptsUnderstandTool:
    """`concepts_understand` {query, k?}: semantic search over this
    project's concept graph -- the closest concepts to `query` plus their
    one-hop neighbours. Read-only. Call this before exploring a subsystem
    the project may already have concepts for."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content)
        store = _store_for(ctx)
        if store is None:
            return _no_project("concepts_understand")
        query = str(args.get("query") or "").strip()
        if not query:
            return {"error": "concepts_understand: `query` is required", "exit_code": 1}
        k = args.get("k")
        result = store.understand(query, k=int(k) if k else pc.DEFAULT_UNDERSTAND_K)
        lines = [f"{c['name']} [{c['kind']}] (score {c['score']}): {c['summary']}" for c in result["concepts"]]
        if result["neighbors"]:
            lines.append("Related: " + ", ".join(n["name"] for n in result["neighbors"]))
        return {"output": "\n".join(lines) or "(no concepts recorded yet)", "exit_code": 0, "result": result}


class ConceptGetTool:
    """`concept_get` {id}: one concept's full detail -- summary, details,
    refs, incoming/outgoing edges, children. Read-only."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content)
        store = _store_for(ctx)
        if store is None:
            return _no_project("concept_get")
        cid = str(args.get("id") or args.get("concept_id") or "").strip()
        if not cid:
            return {"error": "concept_get: `id` is required", "exit_code": 1}
        item = store.get_concept(cid)
        if item is None:
            return {"error": f"concept_get: {cid} not found", "exit_code": 1, "error_class": "project_concepts.not_found"}
        lines = [f"{item['name']} [{item['kind']}]", item["summary"]]
        if item.get("refs"):
            lines.append("Refs: " + ", ".join(item["refs"]))
        return {"output": "\n".join(x for x in lines if x), "exit_code": 0, "concept": item}


class ConceptsRootsTool:
    """`concepts_roots`: top-level concepts (no parent) of this project,
    with their child counts. Read-only. A good first call when starting a
    task in an unfamiliar project."""

    async def execute(self, content: str, ctx: dict) -> dict:
        store = _store_for(ctx)
        if store is None:
            return _no_project("concepts_roots")
        roots = store.list_roots()
        lines = [f"{r['name']} [{r['kind']}] ({r['child_count']} children): {r['summary']}" for r in roots]
        return {"output": "\n".join(lines) or "(no concepts recorded yet)", "exit_code": 0, "concepts": roots}


class ConceptUpsertTool:
    """`concept_upsert` {name, kind, summary, details?, refs?, parent_id?, id?}:
    create a new concept, or update an existing one when `id` matches one
    already in this project. `kind` is one of feature/module/pattern/
    config/decision/component. `refs` should cite the files/symbols this
    concept is grounded in (e.g. 'src/embeddings.py', 'src/embeddings.py@
    get_embedding_client') -- these are what later staleness checks ground
    against. Use this whenever you work out what a subsystem is, why it
    exists, or how it fits together, so the next session does not have to
    re-derive it."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content)
        store = _store_for(ctx)
        if store is None:
            return _no_project("concept_upsert")
        name = str(args.get("name") or "").strip()
        kind = str(args.get("kind") or "").strip()
        if not name or not kind:
            return {"error": "concept_upsert: `name` and `kind` are required", "exit_code": 1}
        try:
            item = store.upsert_concept(
                name=name, kind=kind, summary=str(args.get("summary") or ""),
                details=str(args.get("details") or ""),
                refs=args.get("refs") if isinstance(args.get("refs"), list) else None,
                parent_id=str(args.get("parent_id") or "").strip() or None,
                concept_id=str(args.get("id") or "").strip() or None,
            )
        except pc.ProjectConceptsError as exc:
            return _pc_error("concept_upsert", exc)
        return {"output": f"Recorded concept {item['id']} ({item['kind']}): {item['name']}",
                "exit_code": 0, "concept": item}


class ConceptLinkTool:
    """`concept_link` {src, dst, rel, note?}: attach a typed relation
    between two concepts already in this project -- `connects_to`,
    `depends_on`, `implements`, `calls`, or `configured_by`. Both ids must
    already exist (create them with `concept_upsert` first)."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content)
        store = _store_for(ctx)
        if store is None:
            return _no_project("concept_link")
        src = str(args.get("src") or "").strip()
        dst = str(args.get("dst") or "").strip()
        rel = str(args.get("rel") or "").strip()
        if not src or not dst or not rel:
            return {"error": "concept_link: `src`, `dst` and `rel` are required", "exit_code": 1}
        try:
            edge = store.link(src, dst, rel, note=str(args.get("note") or ""))
        except pc.ProjectConceptsError as exc:
            return _pc_error("concept_link", exc)
        return {"output": f"Linked {src} --{rel}--> {dst}", "exit_code": 0, "edge": edge}


class ConceptRemoveTool:
    """`concept_remove` {id}: soft-delete a concept (and its edges) -- it
    stops showing up in `concepts_understand`/`concepts_roots`, but its
    history is kept. Use when a concept describes something that was
    removed from the project or was simply wrong."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content)
        store = _store_for(ctx)
        if store is None:
            return _no_project("concept_remove")
        cid = str(args.get("id") or args.get("concept_id") or "").strip()
        if not cid:
            return {"error": "concept_remove: `id` is required", "exit_code": 1}
        removed = store.remove_concept(cid)
        if not removed:
            return {"error": f"concept_remove: {cid} not found", "exit_code": 1, "error_class": "project_concepts.not_found"}
        return {"output": f"Removed concept {cid}", "exit_code": 0, "id": cid}
