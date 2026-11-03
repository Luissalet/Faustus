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
GET  /api/code-graph/floorplan      ?root=&project_id=  (last cached floorplan)
POST /api/code-graph/floorplan      {root?, project_id?, labels?, max_labels?}
POST /api/code-graph/floorplan/blueprint {goal, root?, project_id?, labels?}
GET  /api/code-graph/floorplan/export ?root=&project_id=&download=  (self-contained HTML)
"""
import logging
import os
from contextlib import contextmanager

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
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


class FloorplanBody(BaseModel):
    root: str = ""
    project_id: str = ""
    labels: str = "model"
    max_labels: int = 60


class BlueprintBody(BaseModel):
    goal: str
    root: str = ""
    project_id: str = ""
    labels: str = "model"


@contextmanager
def _project_workspace(request: Request, root: str):
    """Bind a project's own work folder as the active workspace for this
    request, so the Studio floorplan of a project works on that folder the
    same way an agent turn in the project would. Only a folder that is the
    `workspace` of one of the caller's projects is bound; any other root is
    left to the usual confinement in `code_graph.query._root`."""
    tokens = None
    try:
        from services.projects import get_store
        from src import tool_execution as te
        from src.auth_helpers import effective_user
        wanted = te.vet_project_root(root or "")
        if wanted and os.path.isdir(wanted):
            for project in get_store().list(effective_user(request)) or []:
                raw = project.get("workspace") if isinstance(project, dict) else getattr(project, "workspace", "")
                ws = te.vet_project_root(str(raw or ""))
                if ws and os.path.normcase(ws) == os.path.normcase(wanted):
                    tokens = (te._active_workspace.set(ws), te._active_workspace_roots.set((ws,)))
                    break
    except Exception as exc:  # noqa: BLE001 - falls back to the default confinement
        logger.debug("floorplan: project workspace binding skipped: %s", exc)
    try:
        yield
    finally:
        if tokens:
            from src import tool_execution as te
            te._active_workspace.reset(tokens[0])
            te._active_workspace_roots.reset(tokens[1])


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

    @router.get("/floorplan")
    def floorplan_open(request: Request, root: str = "", project_id: str = ""):
        require_admin(request)
        with _project_workspace(request, root):
            return _guarded(code_graph.floorplan_open, root, project_id=project_id)

    @router.post("/floorplan")
    def floorplan_generate(body: FloorplanBody, request: Request):
        require_admin(request)
        with _project_workspace(request, body.root):
            return _guarded(code_graph.floorplan_generate, body.root, project_id=body.project_id,
                            labels=body.labels, max_labels=body.max_labels)

    @router.post("/floorplan/blueprint")
    def floorplan_blueprint(body: BlueprintBody, request: Request):
        require_admin(request)
        with _project_workspace(request, body.root):
            return _guarded(code_graph.floorplan_blueprint, body.root, body.goal,
                            project_id=body.project_id, labels=body.labels)

    @router.get("/floorplan/export")
    def floorplan_export(request: Request, root: str = "", project_id: str = "",
                         download: bool = False):
        require_admin(request)
        with _project_workspace(request, root):
            # Shown in the browser the page runs under the app's CSP, which only
            # executes nonced inline scripts; the downloaded file needs none.
            nonce = "" if download else str(getattr(request.state, "csp_nonce", "") or "")
            res = _guarded(code_graph.floorplan_export, root, project_id=project_id,
                           studio_base=str(request.base_url).rstrip("/"), nonce=nonce)
        if res.get("exit_code") != 0:
            raise HTTPException(status_code=404, detail=res.get("output") or "no floorplan yet")
        headers = {}
        if download:
            headers["Content-Disposition"] = 'attachment; filename="floorplan.html"'
        return HTMLResponse(res["html"], headers=headers)

    return router
