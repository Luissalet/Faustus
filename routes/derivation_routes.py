"""routes/derivation_routes.py — HTTP surface for learned recipes, derived
results with selective rebuild, and the decision/effect map. Thin wrappers over
`src/derivation_ops.py`, the same dispatcher the chat tools and the MCP server
use. `require_admin` on every endpoint (the graph hashes files and can register
results in Atlas); data is scoped to the signed-in owner.

POST /api/recipes/learn                 {run_id | session_id | workflow_run_id | request+steps, title?, params_hint?, turn?}
POST /api/recipes/{recipe_id}/try       {params}   dry run on a second case, nothing executed
POST /api/recipes/{recipe_id}/promote   only after a clean try
GET  /api/recipes/search                ?query=&limit=
GET  /api/derivations                   graphs of the owner
GET  /api/derivations/{graph}           nodes, state, Atlas project
GET  /api/derivations/{graph}/plan      ?target=   (target repeatable) stale nodes and why
POST /api/derivations/{graph}/{action}  define | remove | rebuild | complete | base_dir | atlas   {node, op, inputs, params, outputs, target, ...}
GET  /api/decision-map/{graph}
GET  /api/decision-map/{graph}/affected-by  ?change=&invalidate=
POST /api/decision-map/{graph}/{action} add | link | unlink | remove  {id, kind, title, from, to, relation}
"""
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from core.middleware import require_admin
from src import derivation_ops
from src.auth_helpers import effective_user


def _reply(result: Dict[str, Any]):
    if result.get("error_class"):
        cls = str(result["error_class"])
        status = 404 if cls.endswith("not_found") or cls.endswith("unknown_node") or cls.endswith("unknown_ref") else 400
        return JSONResponse(status_code=status, content=result)
    result.pop("exit_code", None)
    return result


async def _body(request: Request) -> Dict[str, Any]:
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def setup_derivation_routes() -> APIRouter:
    router = APIRouter(tags=["derivations"])

    def owner_of(request: Request) -> str:
        require_admin(request)
        return effective_user(request) or ""

    @router.post("/api/recipes/learn")
    async def learn_recipe(request: Request):
        owner = owner_of(request)
        body = await _body(request)
        body["action"] = "propose"
        return _reply(derivation_ops.run_recipe(owner, body))

    @router.get("/api/recipes/search")
    async def search_recipes(request: Request, query: str = Query(...), limit: int = 10):
        owner = owner_of(request)
        return _reply(derivation_ops.run_recipe(owner, {"action": "search", "query": query, "limit": limit}))

    @router.post("/api/recipes/{recipe_id}/try")
    async def try_recipe(request: Request, recipe_id: str):
        owner = owner_of(request)
        body = await _body(request)
        return _reply(derivation_ops.run_recipe(owner, {"action": "try", "recipe_id": recipe_id,
                                                        "params": body.get("params")}))

    @router.post("/api/recipes/{recipe_id}/promote")
    async def promote_recipe(request: Request, recipe_id: str):
        owner = owner_of(request)
        return _reply(derivation_ops.run_recipe(owner, {"action": "promote", "recipe_id": recipe_id}))

    @router.get("/api/derivations")
    async def list_graphs(request: Request):
        return _reply(derivation_ops.run_graph(owner_of(request), {"action": "list"}))

    @router.get("/api/derivations/{graph}")
    async def show_graph(request: Request, graph: str):
        return _reply(derivation_ops.run_graph(owner_of(request), {"action": "show", "graph": graph}))

    @router.get("/api/derivations/{graph}/plan")
    async def plan_graph(request: Request, graph: str, target: Optional[List[str]] = Query(None)):
        t: Any = None if not target else (target[0] if len(target) == 1 else target)
        return _reply(derivation_ops.run_graph(owner_of(request), {"action": "plan", "graph": graph, "target": t}))

    @router.post("/api/derivations/{graph}/{action}")
    async def graph_action(request: Request, graph: str, action: str):
        owner = owner_of(request)
        body = await _body(request)
        body.update(action=action, graph=graph)
        return _reply(derivation_ops.run_graph(owner, body))

    @router.get("/api/decision-map/{graph}")
    async def show_map(request: Request, graph: str):
        return _reply(derivation_ops.run_map(owner_of(request), {"action": "show", "graph": graph}))

    @router.get("/api/decision-map/{graph}/affected-by")
    async def affected_by(request: Request, graph: str, change: str = Query(...), invalidate: bool = False):
        return _reply(derivation_ops.run_map(owner_of(request), {"action": "affected_by", "graph": graph,
                                                                 "change": change, "invalidate": invalidate}))

    @router.post("/api/decision-map/{graph}/{action}")
    async def map_action(request: Request, graph: str, action: str):
        owner = owner_of(request)
        body = await _body(request)
        body.update(action=action, graph=graph)
        return _reply(derivation_ops.run_map(owner, body))

    return router
