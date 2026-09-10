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

import asyncio
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
    from src.workflows.runtime import production_handlers
    return WorkflowEngine(production_handlers(), store)


def setup_workflows_routes():
    router = APIRouter(prefix="/api/workflows", tags=["workflows"])
    from routes.workflow_credentials_routes import setup_workflow_credentials_routes
    router.include_router(setup_workflow_credentials_routes())
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
        from core import middleware
        from src.owner_identity import effective_storage_owner
        owner = effective_storage_owner(payload.get('owner') or getattr(request.state, 'current_user', None),
                                        auth_is_disabled=middleware.auth_disabled())
        if not owner:
            raise HTTPException(400, 'A workflow needs an owner for its outputs and approval requests')
        # AUTO-02/lot-36: `WorkflowStore.create_run` already accepts and
        # stores `budget_preset`/`permissions` (see its own docstring — the
        # `__policy__` reserved inputs key, enforced by `WorkflowEngine.
        # advance`); this route just never forwarded them. Optional, same as
        # every other field on this payload — a caller that sends neither
        # (every caller before this lot) starts a run with no declared
        # policy, exactly as before.
        budget_preset = str(payload.get("budget_preset") or "")
        if budget_preset:
            from src.autonomy_budget import PRESETS as _WORKFLOW_BUDGET_PRESETS
            if budget_preset not in _WORKFLOW_BUDGET_PRESETS:
                raise HTTPException(400, f"budget_preset must be one of {list(_WORKFLOW_BUDGET_PRESETS)}")
        permissions = payload.get("permissions")
        if permissions is not None and not isinstance(permissions, list):
            raise HTTPException(400, "permissions must be a list of strings")
        created = store.create_run(
            definition,
            owner=owner,
            project_id=str(payload.get("project_id") or ""),
            trigger=str(payload.get("trigger") or "manual"),
            inputs=payload.get("inputs") or {},
            dedupe_key=str(payload.get("dedupe_key") or ""),
            budget_preset=budget_preset,
            permissions=permissions)
        if payload.get("advance"):
            created["result"] = await asyncio.to_thread(_engine(store).advance, created["run_id"])
        return created

    @router.get('/runs')
    def list_runs(request: Request, limit: int = 80):
        require_admin(request)
        if not 1 <= limit <= 200:
            raise HTTPException(status_code=400, detail='limit must be between 1 and 200')
        from core.database import SessionLocal, WorkflowRunRow, NodeRunRow
        from sqlalchemy import case
        import json
        with SessionLocal() as db:
            rows = db.query(WorkflowRunRow).order_by(
                case((WorkflowRunRow.status.in_(('running', 'paused', 'pending')), 0), else_=1),
                WorkflowRunRow.created_at_iso.desc(), WorkflowRunRow.id).limit(limit).all()
            nodes = db.query(NodeRunRow).filter(
                NodeRunRow.workflow_run_id.in_([row.id for row in rows])).all() if rows else []
            by_run = {}
            for node in nodes:
                previous = by_run.setdefault(node.workflow_run_id, {}).get(node.node_id)
                if previous and previous['attempt'] > node.attempt:
                    continue
                try:
                    result = json.loads(node.result_json or '{}')
                    if not isinstance(result, dict):
                        result = {}
                except (ValueError, TypeError):
                    result = {}
                by_run[node.workflow_run_id][node.node_id] = {
                    'id': node.node_id, 'status': node.status, 'attempt': node.attempt,
                    'reason': node.reason or '', 'approval_id': node.approval_id or '',
                    'wake_at': str(result.get('wake_at') or ''),
                    'artifacts': [{'id': item['id'], 'label': str(item.get('label') or item['id'])[:300]}
                                  for item in (result.get('artifacts') or [])[:100]
                                  if isinstance(item, dict) and isinstance(item.get('id'), str)]
                                 if isinstance(result.get('artifacts'), list) else [],
                }
            runs = []
            for row in rows:
                try:
                    definition = json.loads(row.definition_json)
                    title = str(definition.get('title') or row.workflow_id)
                    declared = definition.get('nodes') or []
                    if not isinstance(declared, list):
                        declared = []
                except (ValueError, TypeError, AttributeError):
                    title, declared = row.workflow_id, []
                states = by_run.get(row.id, {})
                steps = [{**states.get(node['id'], {'id': node['id'], 'status': 'pending', 'attempt': 0}),
                          'title': str(node.get('title') or node['id']),
                          'needs': node.get('needs') or []} for node in declared
                         if isinstance(node, dict) and isinstance(node.get('id'), str)]
                runs.append({'id': row.id, 'title': title, 'status': row.status,
                             'workflow_id': row.workflow_id, 'project_id': row.project_id or '',
                             'started_at': row.started_at or row.created_at_iso,
                             'finished_at': row.ended_at, 'reason': row.reason or '', 'nodes': steps})
        return {'ok': True, 'runs': runs}

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
        raw_limit = payload.get('max_nodes', 50)
        if isinstance(raw_limit, bool) or not isinstance(raw_limit, int) or not 1 <= raw_limit <= 500:
            raise HTTPException(status_code=400, detail='max_nodes must be an integer from 1 to 500')
        result = await asyncio.to_thread(_engine(store).advance, run_id, max_nodes=raw_limit)
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
        result = await asyncio.to_thread(_engine(store).resume, run_id, node_id)
        if not result.get("ok"):
            raise HTTPException(status_code=409, detail=result.get("reason"))
        return result

    @router.post("/runs/{run_id}/nodes/{node_id}/retry")
    async def retry_node(run_id: str, node_id: str, request: Request):
        """AUTO-03: re-run one finished, non-effectful node — the debugging
        move a DAG needs (re-extract after fixing the input) without ever
        repeating a `skill`/`artifact_store`/`deliver` node that already
        confirmed. See `WorkflowStore.retry_node` for why the two cases are
        not the same button."""
        require_admin(request)
        loaded = store.get_run(run_id)
        if loaded is None:
            raise HTTPException(status_code=404, detail=f"no run {run_id}")
        result = store.retry_node(run_id, node_id, loaded["definition"])
        if not result.get("ok"):
            raise HTTPException(status_code=409, detail=result.get("reason"))
        payload = await _optional_json(request)
        if payload.get("advance"):
            result["result"] = await asyncio.to_thread(_engine(store).advance, run_id)
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
        if not store.set_run_status(run_id, "cancelled",
                                    reason=str(payload.get("reason") or "cancelled by a person")):
            current = store.get_run(run_id)
            if current is None:
                raise HTTPException(status_code=404, detail=f"no run {run_id}")
            return {"ok": True, "reason": f"already_{current['run'].status}",
                    "run_id": run_id, "status": current['run'].status}
        return {"ok": True, "run_id": run_id, "status": "cancelled"}

    return router


async def _optional_json(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}
