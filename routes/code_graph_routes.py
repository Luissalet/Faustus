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
GET  /api/code-graph/snippet        ?symbol=&root=
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

    @router.get("/snippet")
    def snippet(request: Request, symbol: str = Query(...), root: str = "", project_id: str = ""):
        require_admin(request)
        return _guarded(code_graph.snippet, symbol, workspace=root, project_id=project_id)

    return router
