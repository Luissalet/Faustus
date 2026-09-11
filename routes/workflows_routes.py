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


def _skill_call_history_from_disk() -> dict:
    """`DATA_DIR/skill_call_history.json`, CMP-08's second (fallback) source
    of a composite skill's real call shape — read only if the file exists,
    never written by this module. Shape: `{skill_id: {model_calls,
    external_ops, tokens_in, tokens_out, samples}}`. Any read/parse failure
    is swallowed to `{}` — a missing or malformed history file must never
    turn a cost estimate into a 500; it just means those skills stay
    `calls_profile_source: "unknown"`, same as if the file never existed."""
    import json
    import os

    from src.constants import DATA_DIR
    path = os.path.join(DATA_DIR, "skill_call_history.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        logger.debug("skill_call_history.json unreadable — treated as absent", exc_info=True)
        return {}


def _detail_inputs_from_payload(payload: dict) -> dict:
    """The optional CMP-08 inputs `estimate_detailed`/`plan_compare.compare`
    take: everything the pure estimator refuses to fetch itself (see
    `workflow_cost_estimate.estimate_detailed`'s docstring — no network, no
    reaching into ambient runtime state from a pure function). A caller that
    already has an OpenRouter catalogue slice, a skill's declared
    `calls_profile`, or a precomputed local-latency figure passes it in
    here; nothing is looked up behind the caller's back except the on-disk
    call-history fallback, which is always attempted (`{}` if absent)."""
    capability_pricing = payload.get("capability_pricing")
    skill_calls_profiles = payload.get("skill_calls_profiles")
    local_latency = payload.get("local_latency")
    return {
        "capability_pricing": capability_pricing if isinstance(capability_pricing, dict) else None,
        "skill_calls_profiles": skill_calls_profiles if isinstance(skill_calls_profiles, dict) else None,
        "skill_call_history": _skill_call_history_from_disk(),
        "local_latency": local_latency if isinstance(local_latency, dict) else None,
    }


def _prices_from_payload(payload: dict) -> dict:
    """`prices: {model_id: {"prompt_usd_per_1k", "completion_usd_per_1k"}}`,
    shared by `/estimate` and `/preflight` so the two never parse it
    differently."""
    from src.workflow_cost_estimate import ModelPrice
    raw_prices = payload.get("prices")
    prices: dict = {}
    if isinstance(raw_prices, dict):
        for model_id, raw_price in raw_prices.items():
            if not isinstance(raw_price, dict):
                continue
            try:
                prices[str(model_id)] = ModelPrice(
                    prompt_usd_per_1k=float(raw_price.get("prompt_usd_per_1k", 0.0)),
                    completion_usd_per_1k=float(raw_price.get("completion_usd_per_1k", 0.0)),
                )
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail=f"prices.{model_id}: expected numbers")
    return prices


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

    @router.post("/simulate")
    async def simulate_workflow(request: Request):
        """CMP-07: a structural, round-by-round walk of the definition — no
        handler is called, no store is touched, no network of any kind (see
        `src/workflows/simulate.py`'s module docstring). `choices:
        {node_id: bool}` lets the caller assume one outcome for a
        `condition`/`human_approval` node while exploring; a node named in
        neither `choices` nor reachable at all is reported in
        `awaiting_choice`/`not_reached` rather than guessed past.
        `rounds_max` (default 25) bounds how many dependency layers deep the
        walk goes — a real cap, not a schedule; hitting it is reported in
        `warnings`, never silent."""
        require_admin(request)
        payload = await _json_object(request)
        definition = _definition_or_400(payload.get("definition", payload))
        raw_choices = payload.get("choices")
        choices: dict = {}
        if raw_choices is not None:
            if not isinstance(raw_choices, dict):
                raise HTTPException(status_code=400, detail="choices must be an object of {node_id: bool}")
            for node_id, value in raw_choices.items():
                if not isinstance(value, bool):
                    raise HTTPException(status_code=400, detail=f"choices.{node_id} must be true or false")
                choices[str(node_id)] = value
        raw_rounds = payload.get("rounds_max", 25)
        if isinstance(raw_rounds, bool) or not isinstance(raw_rounds, int) or not 1 <= raw_rounds <= 200:
            raise HTTPException(status_code=400, detail="rounds_max must be an integer from 1 to 200")
        from src.workflows.simulate import simulate as compute_simulation
        result = compute_simulation(definition, choices=choices, rounds_max=raw_rounds)
        return {"ok": True, "simulation": result.to_dict()}

    @router.post("/mermaid")
    async def mermaid(request: Request):
        """Lote A4: a `flowchart TD` of the definition's nodes and `needs`
        edges. Pure — parses the same way `/validate` does, so an invalid
        definition is refused with the same field-level message rather than
        drawn as if it could run."""
        require_admin(request)
        payload = await _json_object(request)
        definition = _definition_or_400(payload.get("definition", payload))
        from src.topology_export import workflow_to_mermaid
        return {"ok": True, "mermaid": workflow_to_mermaid(definition)}

    @router.post("/estimate")
    async def estimate_cost(request: Request, detail: int = 0):
        """Lote A4: a cost estimate for one run of the definition. Pure — no
        model price catalogue is looked up automatically (see
        `src/workflow_cost_estimate.py`'s docstring for why); pass
        `prices: {model_id: {"prompt_usd_per_1k", "completion_usd_per_1k"}}`
        to price the models named on `skill` nodes' `config.model`.

        CMP-08: `?detail=1` returns `estimate_detailed()`'s shape instead —
        separate `node_activations`/`model_calls`/`external_ops`/`tokens`
        accounts, structured prices, and a `structural_bounds`/
        `forecast_with_assumptions`/`measured` split (see that function's
        docstring for every optional input this route threads through from
        the request body: `capability_pricing`, `skill_calls_profiles`,
        `local_latency`; `run_id`, if the run exists, supplies `measured`
        from `WorkflowStore.usage_so_far`). The plain (non-detail) shape is
        unchanged — `preflight.py` and every existing caller of this route
        still gets exactly what they got before."""
        require_admin(request)
        payload = await _json_object(request)
        definition = _definition_or_400(payload.get("definition", payload))
        prices = _prices_from_payload(payload)
        if not detail:
            from src.workflow_cost_estimate import estimate as compute_estimate
            result = compute_estimate(definition, prices=prices or None)
            return {"ok": True, "estimate": result.to_dict()}

        from src.workflow_cost_estimate import estimate_detailed as compute_detailed
        run_measured = None
        raw_run_id = payload.get("run_id")
        if isinstance(raw_run_id, str) and raw_run_id:
            loaded = store.get_run(raw_run_id)
            if loaded is not None:
                run_measured = store.usage_so_far(raw_run_id)
        result = compute_detailed(definition, prices=prices or None,
                                  run_measured=run_measured,
                                  **_detail_inputs_from_payload(payload))
        return {"ok": True, "estimate": result.to_dict()}

    @router.post("/compare-plans")
    async def compare_plans(request: Request):
        """CMP-08: `plan_compare.compare` — several candidate definitions for
        the same `goal`, one table, each cell tagged `computed`/`estimated`/
        `unknown` instead of one blended number per plan. Body:
        `{goal, plans: [{id, label?, definition}, ...], prices?,
        capability_pricing?, skill_calls_profiles?, local_latency?}` — the
        same optional inputs `/estimate?detail=1` accepts, shared across
        every plan in the comparison so none is priced more generously than
        another. A plan whose `definition` does not parse is reported in
        `errors` rather than failing the whole request (see
        `src/plan_compare.py`)."""
        require_admin(request)
        payload = await _json_object(request)
        raw_plans = payload.get("plans")
        if not isinstance(raw_plans, list) or not raw_plans:
            raise HTTPException(status_code=400, detail="plans must be a non-empty list")
        plans = []
        for i, raw_plan in enumerate(raw_plans):
            if not isinstance(raw_plan, dict):
                raise HTTPException(status_code=400, detail=f"plans[{i}] must be an object")
            plans.append(raw_plan)
        prices = _prices_from_payload(payload)
        from src.plan_compare import compare as compute_comparison
        result = compute_comparison(str(payload.get("goal") or ""), plans,
                                    prices=prices or None,
                                    **_detail_inputs_from_payload(payload))
        return {"ok": True, "comparison": result.to_dict()}

    @router.post("/preflight")
    async def preflight_definition(request: Request):
        """ADP-16: connections, tools, permissions, inputs, outputs, human
        waits, a token estimate and a cost estimate for the definition — cero
        LLM/scripts/descargas (see `src/workflows/preflight.py`'s module
        docstring). Same `prices` shape as `/estimate`; `installed_models`,
        when given, only annotates `token_estimate.per_node` and changes
        nothing else."""
        require_admin(request)
        payload = await _json_object(request)
        definition = _definition_or_400(payload.get("definition", payload))
        from src.workflows.preflight import preflight as compute_preflight
        prices = _prices_from_payload(payload)
        raw_installed = payload.get("installed_models")
        if raw_installed is not None and not (
                isinstance(raw_installed, list) and all(isinstance(m, str) for m in raw_installed)):
            raise HTTPException(status_code=400, detail="installed_models must be a list of strings")
        result = compute_preflight(definition, prices=prices or None,
                                   installed_models=raw_installed)
        return {"ok": True, "preflight": result.to_dict()}

    @router.post("/export")
    async def export_definition(request: Request):
        """ADP-17: the canonical, versioned envelope (`export_canonical`) for
        a definition passed in the body — round-trips through `/import`
        unchanged (see `docs/api/topology.md`). `layout`/`design_only`/
        `provenance` are optional and purely informational."""
        require_admin(request)
        payload = await _json_object(request)
        definition = _definition_or_400(payload.get("definition", payload))
        from src.workflows.interchange import export_canonical
        layout = payload.get("layout")
        design_only = payload.get("design_only")
        provenance = payload.get("provenance")
        if layout is not None and not isinstance(layout, dict):
            raise HTTPException(status_code=400, detail="layout must be an object")
        if design_only is not None and not isinstance(design_only, list):
            raise HTTPException(status_code=400, detail="design_only must be a list")
        if provenance is not None and not isinstance(provenance, dict):
            raise HTTPException(status_code=400, detail="provenance must be an object")
        exported = export_canonical(definition, layout, design_only=design_only,
                                    provenance=provenance)
        return {"ok": True, "export": exported}

    @router.get("/runs/{run_id}/export")
    def export_run_definition(run_id: str, request: Request):
        """ADP-17: the same canonical envelope, for the definition a stored
        run actually ran under — the version pinned on the run, not whatever
        the workflow's source looks like today (see `WorkflowRun`'s own
        docstring on why the version is stored, not looked up)."""
        require_admin(request)
        loaded = store.get_run(run_id)
        if loaded is None:
            raise HTTPException(status_code=404, detail=f"no run {run_id}")
        from src.workflows.interchange import export_canonical
        return {"ok": True, "run_id": run_id, "export": export_canonical(loaded["definition"])}

    @router.post("/import")
    async def import_definition(request: Request):
        """ADP-17: import a workflow drafted elsewhere — this module's own
        canonical envelope, or an aigraphstudio-shaped payload (see
        `src/workflows/interchange.py`'s module docstring for the accepted
        shapes and the mapped/design-only node-type split). Always 200: an
        unrecognized format, a cycle or an unsupported node type is a normal
        outcome here (`executable: false`, same as `/validate` answering a
        bad definition with `ok: false` rather than a 4xx), never a crash."""
        require_admin(request)
        payload = await _json_object(request)
        from src.workflows.interchange import import_external
        result = import_external(payload)
        return {"ok": True, **result}

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
