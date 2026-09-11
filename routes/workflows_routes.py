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


#: The counters that make a `skill_call_history.json` entry actually usable
#: as a `CallsProfile` (W3-C — see `_skill_call_history_from_disk` below).
_CALL_HISTORY_COUNT_KEYS = ("model_calls", "external_ops", "tokens_in", "tokens_out")


def _skill_call_history_from_disk() -> dict:
    """`DATA_DIR/skill_call_history.json`, CMP-08's second (fallback) source
    of a composite skill's real call shape. Written by
    `src/workflows/skills.py` at the end of every script-skill run — see
    that module's own docstring for why the entry there is usually `{"runs":
    N}` and nothing else: a container script execution never observes its
    own model-call/token counts. Shape on disk: `{skill_id: {runs,
    model_calls?, external_ops?, tokens_in?, tokens_out?}}`.

    W3-C: an entry that has never gained a real count (only `runs`) is
    dropped here, not handed to `workflow_cost_estimate.calls_profile_from_mapping`
    as-is — that function reads a missing key as `0` via `.get(key, 0)`,
    which would turn "we do not know this skill's call shape" into "this
    skill makes 0 model calls", the exact `unknown`-read-as-`0`/`free`
    mistake CMP-08 exists to refuse. Dropping the entry instead leaves
    `estimate_detailed` falling through to `calls_profile_source: "unknown"`,
    same as if the file never mentioned this skill.

    Any read/parse failure is swallowed to `{}` — a missing or malformed
    history file must never turn a cost estimate into a 500."""
    import json
    import os

    from src.constants import DATA_DIR
    path = os.path.join(DATA_DIR, "skill_call_history.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
    except FileNotFoundError:
        return {}
    except Exception:
        logger.debug("skill_call_history.json unreadable — treated as absent", exc_info=True)
        return {}
    return {
        skill_id: entry for skill_id, entry in data.items()
        if isinstance(entry, dict)
        and any(entry.get(key) is not None for key in _CALL_HISTORY_COUNT_KEYS)
    }


def _declared_calls_profiles_from_manifests(definition, *, owner: str, project_id: str) -> dict:
    """W3-C: `calls_profile` declared directly on a project's own `SKILL.md`
    files (`src.skills_runtime.bridge.calls_profile_from_frontmatter`, read
    into `SkillManifest.calls_profile` — `src/contracts/skill.py`), found the
    same way `src/workflows/skills.py::run` finds a script skill: walking
    the project's workspace with `src.skills_runtime.discovery`.

    Only fills in a skill the caller did NOT already name in
    `skill_calls_profiles` — that payload field is an explicit answer and
    always wins (see the merge in `_detail_inputs_from_payload`). Missing
    `owner`/`project_id`, an unknown project, or no workspace on disk all
    resolve to `{}` rather than an error: this is a convenience default for
    the common case (the workflow's own project has the skill installed),
    never a requirement — `skill_calls_profiles`/`skill_call_history` keep
    working with neither."""
    if not owner or not project_id:
        return {}
    skill_ids = {
        str((node.config or {}).get("skill") or "")
        for node in definition.nodes if node.type == "skill"
    }
    skill_ids.discard("")
    if not skill_ids:
        return {}
    import os
    try:
        from services.projects import get_store
        project = get_store().get(project_id, owner=owner)
        workspace = str((project or {}).get("workspace") or "")
    except Exception:
        return {}
    if not workspace or not os.path.isdir(workspace):
        return {}
    from src.skills_runtime import bridge, discovery
    out: dict = {}
    try:
        found_all = list(discovery.discover(workspace))
    except Exception:
        return {}
    for found in found_all:
        if found.error:
            continue
        try:
            text = discovery.read_markdown(found)
            manifest = bridge.manifest_from_markdown(text, source=found.path)
        except Exception:
            continue
        if manifest.id in skill_ids and manifest.calls_profile is not None:
            out.setdefault(manifest.id, manifest.calls_profile.to_dict())
    return out


def _local_latency_snapshot_for_definition(definition) -> dict:
    """W3-INT (CONTRATO_CMP_W2.md § W2-B "punto a cablear por el
    orquestador", `docs/api/topology.md` §Estimate): when the caller did not
    already compute `local_latency` itself, build it from the definition's
    own `skill` nodes rather than leave the detailed estimate's local-latency
    columns permanently blank. No `endpoint_url` is known at this layer (the
    route has no session/runtime context to resolve one from), so each model
    is looked up with `endpoint_url=""` — `local_latency_for` already
    degrades `load`/`queue` to `"unknown"` in that case and only
    `generation_tps` (via `llm_core.local_speed`, which is keyed on model
    alone) can still come back populated. That is strictly better than
    omitting the field, and it costs nothing extra: same "unknown, never
    guessed" contract as a fully-wired caller, just with fewer signals
    available.

    Never raises — a snapshot is a courtesy, not a requirement of the route;
    any failure (a bad definition shape, an unexpected exception inside
    `workflow_cost_estimate`) degrades to `None` (the same as "caller sent
    nothing") rather than failing `/estimate?detail=1`."""
    try:
        from collections.abc import Mapping as _Mapping
        from src.workflow_cost_estimate import local_latency_snapshot
        models: set = set()
        for node in getattr(definition, "nodes", []) or []:
            if getattr(node, "type", None) != "skill":
                continue
            config = node.config if isinstance(getattr(node, "config", None), _Mapping) else {}
            model = str((config or {}).get("model") or "").strip()
            if model:
                models.add(model)
        if not models:
            return {}
        return local_latency_snapshot({model: "" for model in models})
    except Exception:  # noqa: BLE001 — a latency hint must never break the route
        return {}


def _detail_inputs_from_payload(payload: dict, definition=None) -> dict:
    """The optional CMP-08 inputs `estimate_detailed`/`plan_compare.compare`
    take: everything the pure estimator refuses to fetch itself (see
    `workflow_cost_estimate.estimate_detailed`'s docstring — no network, no
    reaching into ambient runtime state from a pure function). A caller that
    already has an OpenRouter catalogue slice, a skill's declared
    `calls_profile`, or a precomputed local-latency figure passes it in
    here.

    Two sources are always attempted behind the caller's back, both
    fallbacks that never override an explicit `skill_calls_profiles` entry:
    the on-disk call-history (`{}` if absent), and — only when `definition`
    is given, i.e. from `/estimate`, not the multi-plan `/compare-plans`
    where no single definition applies — each named skill's own declared
    `calls_profile` from its `SKILL.md` (`{}` if `owner`/`project_id` are
    not on the payload)."""
    capability_pricing = payload.get("capability_pricing")
    skill_calls_profiles_payload = payload.get("skill_calls_profiles")
    local_latency = payload.get("local_latency")
    declared_from_manifests: dict = {}
    if definition is not None:
        owner = str(payload.get("owner") or "")
        project_id = str(payload.get("project_id") or "")
        declared_from_manifests = _declared_calls_profiles_from_manifests(
            definition, owner=owner, project_id=project_id)
    skill_calls_profiles = dict(declared_from_manifests)
    if isinstance(skill_calls_profiles_payload, dict):
        skill_calls_profiles.update(skill_calls_profiles_payload)
    if not isinstance(local_latency, dict) and definition is not None:
        # W3-INT: the caller sent no local_latency of its own — compute a
        # best-effort snapshot from the definition instead of leaving this
        # section unfilled (see _local_latency_snapshot_for_definition).
        local_latency = _local_latency_snapshot_for_definition(definition) or None
    return {
        "capability_pricing": capability_pricing if isinstance(capability_pricing, dict) else None,
        "skill_calls_profiles": skill_calls_profiles or None,
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
        still gets exactly what they got before.

        W3-C: when the payload also names `owner`/`project_id`, a skill
        named on a `skill` node that declares its own `calls_profile` in its
        `SKILL.md` is picked up automatically — `skill_calls_profiles` from
        the body still wins over it for any skill named in both (see
        `_declared_calls_profiles_from_manifests`)."""
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
                                  **_detail_inputs_from_payload(payload, definition))
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
