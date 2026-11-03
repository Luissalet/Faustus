"""routes/code_graph_routes.py — HTTP surface for the code graph (R2, Reach
wave). Thin wrappers over `src.code_graph`, same auth as the neighbouring
tool-catalogue routes (`routes/tool_registry_routes.py`): `require_admin` on
every endpoint, workspace confinement enforced by `code_graph.query._root`
(the same guard `read_file`/grep use — a `root` outside the allowed roots is
a 400, not a 500).

POST /api/code-graph/index          {root?, force?}
GET  /api/code-graph/architecture   ?root=
GET  /api/code-graph/search         ?pattern=&kinds=&limit=&root=&semantic=
GET  /api/code-graph/trace          ?from=&to=&max_depth=&root=
GET  /api/code-graph/changes        ?base_ref=&root=
GET  /api/code-graph/impact         ?symbol=&base_ref=&depth=&root=
GET  /api/code-graph/snippet        ?symbol=&root=
GET  /api/code-graph/communities    ?level=&refresh=&summarize=&root=
GET  /api/code-graph/communities/{id}  ?root=  (id: community id, name substring, or symbol)
GET  /api/code-graph/flows          ?entry=&limit=&sort=&refresh=&root=
GET  /api/code-graph/flows/{id}     ?root=  (id: flow id, or an entry point's name)
GET  /api/code-graph/affected-flows ?symbol=&base_ref=&limit=&root=
GET  /api/code-graph/dossier        ?refresh=&top_flows=&root=&project_id=
GET  /api/code-graph/dossier/diff   ?from_revision=&to_revision=&root=&project_id=
GET  /api/code-graph/locate         ?question=&limit=&root=&project_id=
GET  /api/code-graph/business-route ?start=&end=&root=&project_id=  (runtime_trace: POST body variant below)
POST /api/code-graph/business-route {start, end?, runtime_trace?, root?}
POST /api/code-graph/handoff {root?, out_dir?, run?, timeout?}
POST /api/code-graph/context-pack   {symbols, root?, budget_lines?, base_ref?, known_hashes?}
POST /api/code-graph/drift/snapshot {root?, project_id?, label?}
GET  /api/code-graph/drift          ?baseline_id=&refresh=&root=&project_id=
GET  /api/code-graph/drift/baselines ?limit=&root=&project_id=
"""
import logging

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from core.middleware import require_admin
from src import code_graph

logger = logging.getLogger(__name__)


class IndexBody(BaseModel):
    root: str = ""
    force: bool = False
    project_id: str = ""


class DriftSnapshotBody(BaseModel):
    root: str = ""
    project_id: str = ""
    label: str = ""


class ContextPackBody(BaseModel):
    symbols: list = []
    root: str = ""
    project_id: str = ""
    budget_lines: int = 120
    base_ref: str = ""
    known_hashes: dict = {}


class BusinessRouteBody(BaseModel):
    start: str
    end: str | None = None
    runtime_trace: list | None = None
    root: str = ""
    project_id: str = ""


class HandoffBody(BaseModel):
    root: str = ""
    out_dir: str | None = None
    run: bool = True
    timeout: float = 120
    project_id: str = ""


def _guarded(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("code_graph route failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


def setup_code_graph_routes():
    router = APIRouter(prefix="/api/code-graph")

    @router.post("/index")
    def index_workspace(body: IndexBody, request: Request):
        require_admin(request)
        return _guarded(code_graph.index, body.root, force=body.force, project_id=body.project_id)

    @router.get("/architecture")
    def architecture(request: Request, root: str = "", project_id: str = ""):
        require_admin(request)
        return _guarded(code_graph.get_architecture, root, project_id=project_id)

    @router.get("/search")
    def search(request: Request, pattern: str = Query(...), kinds: str = "", limit: int = 40,
              root: str = "", project_id: str = "", semantic: bool = False):
        require_admin(request)
        kind_list = [k.strip() for k in kinds.split(",") if k.strip()]
        if semantic:
            return _guarded(code_graph.semantic_query, pattern, workspace=root,
                            project_id=project_id, limit=limit)
        return _guarded(code_graph.search_graph, pattern, kinds=kind_list, limit=limit,
                        workspace=root, project_id=project_id)

    @router.get("/trace")
    def trace(request: Request, from_: str = Query(..., alias="from"), to: str = Query(...),
             max_depth: int = 5, root: str = "", project_id: str = ""):
        require_admin(request)
        return _guarded(code_graph.trace_path, from_, to, workspace=root,
                        project_id=project_id, max_depth=max_depth)

    @router.get("/changes")
    def changes(request: Request, base_ref: str = "HEAD", root: str = "", project_id: str = ""):
        require_admin(request)
        return _guarded(code_graph.detect_changes, root, base_ref=base_ref,
                        project_id=project_id)

    @router.get("/impact")
    def impact(request: Request, symbol: str = "", base_ref: str = "HEAD",
               depth: int = 3, root: str = "", project_id: str = "",
               include_history: bool = True, include_risk: bool = True):
        """Consumers and affected tests, using the same graph as MCP and chat."""
        require_admin(request)
        return _guarded(code_graph.impact, symbol, workspace=root,
                        project_id=project_id, base_ref=base_ref, depth=depth,
                        include_history=include_history, include_risk=include_risk)

    @router.get("/snippet")
    def snippet(request: Request, symbol: str = Query(...), root: str = "", project_id: str = ""):
        require_admin(request)
        return _guarded(code_graph.snippet, symbol, workspace=root, project_id=project_id)

    @router.get("/communities")
    def communities(request: Request, level: int = 0, refresh: bool = False,
                    summarize: bool = False, root: str = "", project_id: str = ""):
        require_admin(request)
        return _guarded(code_graph.communities, root, project_id=project_id, level=level,
                        refresh=refresh, summarize=summarize)

    @router.get("/communities/{id}")
    def community_detail(id: str, request: Request, root: str = "", project_id: str = ""):
        require_admin(request)
        return _guarded(code_graph.community, root, id, project_id=project_id)

    @router.get("/flows")
    def flows(request: Request, entry: str = "", limit: int = 20, sort: str = "criticality",
             refresh: bool = False, root: str = "", project_id: str = ""):
        require_admin(request)
        return _guarded(code_graph.flows, root, project_id=project_id, limit=limit, sort=sort,
                        entry=entry or None, refresh=refresh)

    @router.get("/flows/{id}")
    def flow_detail(id: str, request: Request, root: str = "", project_id: str = ""):
        require_admin(request)
        return _guarded(code_graph.flow, root, id, project_id=project_id)

    @router.get("/affected-flows")
    def affected_flows(request: Request, symbol: str = "", base_ref: str = "HEAD",
                       limit: int = 20, root: str = "", project_id: str = ""):
        require_admin(request)
        return _guarded(code_graph.affected_flows, symbol, workspace=root,
                        project_id=project_id, base_ref=base_ref, limit=limit)

    @router.get("/dossier")
    def dossier(request: Request, root: str = "", project_id: str = "", refresh: bool = False,
                top_flows: int = 8):
        require_admin(request)
        return _guarded(code_graph.dossier, root, project_id=project_id, refresh=refresh,
                        top_flows=top_flows)

    @router.get("/dossier/diff")
    def dossier_diff(request: Request, root: str = "", project_id: str = "",
                     from_revision: str = "", to_revision: str = ""):
        require_admin(request)
        return _guarded(code_graph.dossier_diff, root, project_id=project_id,
                        from_revision=from_revision, to_revision=to_revision)

    @router.get("/locate")
    def locate(request: Request, question: str = Query(...), limit: int = 8, root: str = "",
               project_id: str = ""):
        require_admin(request)
        return _guarded(code_graph.locate, root, question, project_id=project_id, limit=limit)

    @router.get("/business-route")
    def business_route(request: Request, start: str = Query(...), end: str = "", root: str = "",
                       project_id: str = ""):
        require_admin(request)
        return _guarded(code_graph.business_route, root, start, end or None, None, project_id=project_id)

    @router.post("/business-route")
    def business_route_post(body: BusinessRouteBody, request: Request):
        require_admin(request)
        return _guarded(code_graph.business_route, body.root, body.start, body.end or None,
                        body.runtime_trace, project_id=body.project_id)

    @router.post("/handoff")
    def handoff(body: HandoffBody, request: Request):
        require_admin(request)
        return _guarded(code_graph.handoff, body.root, body.out_dir, run=body.run,
                        timeout=body.timeout, project_id=body.project_id)

    @router.post("/context-pack")
    def context_pack(body: ContextPackBody, request: Request):
        require_admin(request)
        return _guarded(code_graph.context_pack, body.symbols, body.root,
                        project_id=body.project_id, budget_lines=body.budget_lines,
                        base_ref=body.base_ref, known_hashes=body.known_hashes)

    @router.post("/drift/snapshot")
    def drift_snapshot(body: DriftSnapshotBody, request: Request):
        require_admin(request)
        return _guarded(code_graph.snapshot, body.root, project_id=body.project_id,
                        label=body.label)

    @router.get("/drift/baselines")
    def drift_baselines(request: Request, root: str = "", project_id: str = "", limit: int = 20):
        require_admin(request)
        return _guarded(code_graph.list_baselines, root, project_id=project_id, limit=limit)

    @router.get("/drift")
    def drift(request: Request, root: str = "", project_id: str = "", baseline_id: str = "",
             refresh: bool = False):
        require_admin(request)
        return _guarded(code_graph.drift, root, project_id=project_id,
                        baseline_id=baseline_id, refresh=refresh)

    return router
