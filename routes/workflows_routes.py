"""Durable workflows over HTTP.

The gating follows the same reading as the approvals routes: everything here
is `require_admin`, which the agent's in-process loopback token opens, because
starting and advancing a workflow is ordinary work. The place a person is
actually needed is inside the run — a `human_approval` node — and that is
gated by `require_human` on the approvals routes, where it belongs. Putting a
second human gate here would only mean the model cannot start a workflow that
would have stopped to ask anyway.

Two endpoints are pure and worth having on their own: `validate` parses a
definition and answers with the refusal (the cycle, the missing dependency,
the retry on an effectful node), and `plan` shows the order the nodes would
run in. Both are useful before anything is stored.
"""

import logging

from fastapi import APIRouter, HTTPException, Request

from core.middleware import require_admin
from src.contracts import ContractError, WorkflowDefinition
from src.contracts.base import now_iso
from src.workflows import WorkflowEngine, WorkflowStore, default_handlers, ready_nodes

logger = logging.getLogger(__name__)


async def _json_object(request: Request) -> dict:
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Body must be JSON")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")
    return payload


def _definition_or_400(raw) -> WorkflowDefinition:
    try:
        return WorkflowDefinition.parse(raw)
    except ContractError as e:
        raise HTTPException(status_code=400, detail=f"{e.path}: {e.message}")


def _engine(store: WorkflowStore) -> WorkflowEngine:
    """The handlers a route can offer honestly.

    `deliver`, `skill` and `artifact_store` are left unwired on purpose: this
    process has no mail client and no skill runner bound to a workspace, and a
    node that pretends otherwise is the failure this phase exists to prevent.
    They refuse by name, which is a far better answer than a green run."""
    return WorkflowEngine(default_handlers(), store)


def setup_workflows_routes():
    router = APIRouter(prefix="/api/workflows", tags=["workflows"])
    store = WorkflowStore()

    @router.post("/validate")
    async def validate(request: Request):
        """Pure. Parses a definition and either describes it or says exactly
        what is wrong with it — no run, no rows, no side effects."""
        require_admin(request)
        payload = await _json_object(request)
        raw = payload.get("definition", payload)
        try:
            definition = WorkflowDefinition.parse(raw)
        except ContractError as e:
            return {"ok": False, "field": e.path, "reason": e.message,
                    "got": getattr(e, "got", None)}
        return {"ok": True, "checked_at": now_iso(),
                "workflow": definition.id, "version": definition.version,
                "nodes": len(definition.nodes),
                "roots": list(definition.roots()),
                "fingerprint": definition.fingerprint()}

    @router.post("/plan")
    async def plan(request: Request):
        """What would run first, and what is waiting on what. Pure."""
        require_admin(request)
        payload = await _json_object(request)
        definition = _definition_or_400(payload.get("definition", payload))
        runnable, blocked = ready_nodes(definition, {})
        return {"ok": True, "workflow": definition.id,
                "starts_with": [n.id for n in runnable],
                "waiting": [{"id": n.id, "needs": list(n.needs)}
                            for n in definition.nodes if n.needs],
                "blocked": [n.id for n in blocked]}

    @router.post("/runs")
    async def create_run(request: Request):
        """Start a run. A `dedupe_key` makes a redelivered trigger one run
        rather than two — the same idea as the node keys, one level up."""
        require_admin(request)
        payload = await _json_object(request)
        definition = _definition_or_400(payload.get("definition", payload))
        created = store.create_run(
            definition,
            owner=str(payload.get("owner") or ""),
            project_id=str(payload.get("project_id") or ""),
            trigger=str(payload.get("trigger") or "manual"),
            inputs=payload.get("inputs") or {},
            dedupe_key=str(payload.get("dedupe_key") or ""))
        if payload.get("advance"):
            created["result"] = _engine(store).advance(created["run_id"])
        return created

    @router.get("/runs/{run_id}")
    def get_run(run_id: str, request: Request):
        require_admin(request)
        loaded = store.get_run(run_id)
        if loaded is None:
            raise HTTPException(status_code=404, detail=f"no run {run_id}")
        states = store.node_runs(run_id)
        runnable, blocked = ready_nodes(loaded["definition"], states)
        return {"ok": True, "run": loaded["run"].to_dict(),
                "definition": loaded["definition"].to_dict(),
                "nodes": {nid: st.to_dict() for nid, st in states.items()},
                "runnable_now": [n.id for n in runnable],
                "blocked": [n.id for n in blocked]}

    @router.post("/runs/{run_id}/advance")
    async def advance(run_id: str, request: Request):
        """One pass. Safe to call twice: a node already claimed is read, not
        redone, which is the whole point of the phase."""
        require_admin(request)
        payload = await _optional_json(request)
        result = _engine(store).advance(
            run_id, max_nodes=int(payload.get("max_nodes") or 50))
        if not result.get("ok"):
            raise HTTPException(status_code=404, detail=result.get("reason"))
        return result

    @router.post("/runs/{run_id}/resume/{node_id}")
    async def resume(run_id: str, node_id: str, request: Request):
        """The approval came through — carry on from the paused node.

        This does not grant anything: the answer lives in the approval store,
        and the gate node reads it. Calling this on a card nobody decided
        simply pauses again on the same card."""
        require_admin(request)
        result = _engine(store).resume(run_id, node_id)
        if not result.get("ok"):
            raise HTTPException(status_code=409, detail=result.get("reason"))
        return result

    @router.post("/runs/{run_id}/cancel")
    async def cancel(run_id: str, request: Request):
        require_admin(request)
        payload = await _optional_json(request)
        loaded = store.get_run(run_id)
        if loaded is None:
            raise HTTPException(status_code=404, detail=f"no run {run_id}")
        if loaded["run"].status in ("completed", "failed", "cancelled"):
            return {"ok": True, "reason": f"already_{loaded['run'].status}",
                    "run_id": run_id}
        store.set_run_status(run_id, "cancelled",
                             reason=str(payload.get("reason") or "cancelled by a person"))
        return {"ok": True, "run_id": run_id, "status": "cancelled"}

    return router


async def _optional_json(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}
