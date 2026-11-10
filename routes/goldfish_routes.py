"""routes/goldfish_routes.py -- HTTP surface for the elephant-and-goldfish workflows (src/goldfish.py).

GET    /api/goldfish                      the owner's runs, newest first
GET    /api/goldfish/meta                 flows, reviewer roles, standards, stages, presets, local models
POST   /api/goldfish                      {flow, path?|text?, brief?, context?, context_files?, reviewers?, standards?,
                                           preset?, stages?, max_rounds?, auto_apply?, write_back?, model?,
                                           elephant_model?, models?, panel_mode?, double?, max_findings?}  start a run
GET    /api/goldfish/{id}[?artifact=]     the run (and one of its reports or document versions)
POST   /api/goldfish/{id}/resolve         {mode: auto_accept|continue|decisions, decisions: {finding: accept|skip|overrule}}
POST   /api/goldfish/{id}/cancel
DELETE /api/goldfish/{id}
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from core.middleware import require_admin
from src import goldfish
from src.auth_helpers import effective_user


def _reply(result: Dict[str, Any]):
    if result.get("error_class"):
        status = 404 if str(result["error_class"]).endswith("not_found") else 400
        return JSONResponse(status_code=status, content=result)
    return result


async def _json(request: Request) -> Dict[str, Any]:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    return body if isinstance(body, dict) else {}


def setup_goldfish_routes() -> APIRouter:
    router = APIRouter(tags=["goldfish"])

    def owner_of(request: Request) -> str:
        require_admin(request)
        return effective_user(request) or ""

    @router.get("/api/goldfish")
    async def list_runs(request: Request):
        return _reply(await goldfish.run_action(owner_of(request), {"action": "list"}))

    @router.get("/api/goldfish/meta")
    async def meta(request: Request):
        owner = owner_of(request)
        data = await goldfish.run_action(owner, {"action": "roles"})
        from src import fresh_llm
        data["models"] = fresh_llm.local_models(owner)
        return data

    @router.post("/api/goldfish")
    async def start_run(request: Request):
        owner = owner_of(request)
        body = await _json(request)
        body.pop("wait_s", None)
        return _reply(await goldfish.run_action(owner, {**body, "action": "start"}))

    @router.get("/api/goldfish/{run_id}")
    async def get_run(run_id: str, request: Request, artifact: str = ""):
        return _reply(await goldfish.run_action(owner_of(request), {"action": "status", "id": run_id, "artifact": artifact or None}))

    @router.post("/api/goldfish/{run_id}/resolve")
    async def resolve(run_id: str, request: Request):
        owner = owner_of(request)
        return _reply(await goldfish.run_action(owner, {**(await _json(request)), "action": "resolve", "id": run_id}))

    @router.post("/api/goldfish/{run_id}/cancel")
    async def cancel(run_id: str, request: Request):
        return _reply(await goldfish.run_action(owner_of(request), {"action": "cancel", "id": run_id}))

    @router.delete("/api/goldfish/{run_id}")
    async def delete(run_id: str, request: Request):
        return _reply(await goldfish.run_action(owner_of(request), {"action": "delete", "id": run_id}))

    return router
