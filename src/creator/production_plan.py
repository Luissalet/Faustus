"""production_plan.py — WP22: a brief's storyboard becomes a real, running
production.

Shape follows ``docs/spec/creator/plan/contracts/production-plan.schema.json``
(REFERENCE ONLY, same status as ``documents.py``'s reference schema — see
that module's docstring: this repo's real ids do not carry the reference
schema's ``prj_``/``occ_`` prefixes, so it documents the field shape, not an
enforced contract). ``ProductionPlan`` reimplements the same fields —
``brief`` revision, a DAG of ``nodes`` (adapter/recipe/params/depends_on/
criteria), a ``budget`` policy and an ``approval`` gate — against real ids.

**No parallel authority** (CONTRATO.md rule 1):

* A plan's own persistence is new (``DATA_DIR/creator/production_plans.sqlite3``),
  because nothing else in this repo stores a DAG of production steps.
* Executing a plan does NOT invent a second run engine. ``compile_to_workflow``
  turns the DAG into a real ``src.workflows`` ``WorkflowDefinition`` (one
  ``skill`` node per plan step — the one node type built for exactly this: a
  named effect with a runner, see ``src/workflows/handlers.py::skill_handler``)
  and ``execute`` drives it through the SAME ``WorkflowEngine``/
  ``WorkflowStore`` every other workflow in Faustus runs on. A step's actual
  submit/collect goes through WP10's ``AdapterPort`` + ``production_runs``
  (which itself projects onto ``src.media_runs``'s single ``MediaRunRow``
  authority) — never a second job table.
* ``production_status`` is a PROJECTION, not a second state machine: it reads
  the workflow run's node states and, for any step that reached a real job
  id, the same ``MediaRunRow`` row ``production_runs``/``media_runs`` already
  own. Nothing here decides a step's status independently of those.

**unknown_effect.** Each step node calls ``context["mark_effect"]`` (an
engine-provided hook, ``src/workflows/engine.py``) with ``"pending"``
immediately before the adapter's ``submit()`` call and ``"confirmed"``
immediately after it returns. If the process dies between those two calls,
``src/workflows/store.py``'s own lease-recovery marks that node's effect
``"unknown"`` on the next pass — exactly the ficha's "un nodo = un submit +
collect, con unknown_effect si el proceso muere entre ambos", using
machinery the engine already has rather than reinventing it here.

**Node type + scheduler wiring.** Every plan step compiles to a ``skill``
node whose ``config.skill`` starts with ``creator_plan:`` and whose
``config.creator_plan_step`` is ``True``. ``src/workflows/runtime.py``'s
``production_handlers()`` — the SAME handler set the background
``WorkflowScheduler`` uses to resume ANY running/paused workflow, not only
ones this module started — was given one small, additive hook so that a
production step resumed later (after a restart, or simply on the
scheduler's own tick) is still run by ``creator_step_handler`` here rather
than the generic container-skill runner. See
``<scratchpad>/creator_wave/WP22_wiring.md`` for the exact diff.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from .errors import CreatorError, InvalidOperation

__all__ = [
    "ProductionPlanError", "PlanNotFound", "PlanRevisionConflict", "ProductionNotFound",
    "RETRY_POLICIES", "CRITERION_KINDS",
    "ProductionPlan", "ProductionPlanStore",
    "validate", "estimate", "compile_to_workflow", "execute", "cancel", "production_status",
    "creator_step_handler", "get_store",
]

_BUSY_TIMEOUT_S = 30

RETRY_POLICIES = ("before_effect_only", "idempotent_with_receipt", "manual_reconcile")
CRITERION_KINDS = ("artifact_decodes", "duration_matches", "schema_valid", "human_editorial_acceptance")
CRITERION_STATES = ("pending", "passed", "failed", "needs_review")

#: How long a step waits before the workflow scheduler (or our own bounded
#: poll loop in `execute()`) asks the adapter again. Kept short: fake
#: adapters and ffmpeg-class local jobs finish fast, and a real render's
#: own poll cadence is the adapter's, not this module's.
POLL_SECONDS = 5

#: Bound on how many `advance()` passes `execute()` drives synchronously
#: before handing back a "still running" production rather than blocking the
#: HTTP request forever on a slow real render.
MAX_SYNC_PASSES = 200


class ProductionPlanError(CreatorError):
    pass


class PlanNotFound(ProductionPlanError):
    """No plan with that id, or it belongs to someone else — same 404 shape
    both ways (CONTRATO.md rule 3)."""


class PlanRevisionConflict(ProductionPlanError):
    """`execute()` re-validated the plan against CURRENT input document
    revisions and at least one moved since the plan was created."""

    def __init__(self, changed: Sequence[Dict[str, Any]]) -> None:
        self.changed = list(changed)
        super().__init__(f"{len(self.changed)} input document(s) changed since this plan was created")


class ProductionNotFound(ProductionPlanError):
    pass


# ── the plan itself ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class ProductionPlan:
    schema_version: int
    id: str
    project_id: str
    owner: str
    revision: int
    brief: Dict[str, Any]                 # {document_id, revision}
    available_assets: Tuple[str, ...]
    nodes: Tuple[Dict[str, Any], ...]      # see validate() for the required shape
    criteria: Tuple[Dict[str, Any], ...]
    budget: Dict[str, Any]
    approval: Dict[str, Any]               # {state, plan_digest}
    execution_requested: bool
    created_at: float
    updated_at: float

    def to_public_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version, "id": self.id,
            "project_id": self.project_id, "revision": self.revision,
            "brief": dict(self.brief), "available_assets": list(self.available_assets),
            "nodes": [dict(n) for n in self.nodes],
            "criteria": [dict(c) for c in self.criteria],
            "budget": dict(self.budget), "approval": dict(self.approval),
            "execution_requested": self.execution_requested,
            "created_at": self.created_at, "updated_at": self.updated_at,
        }


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise InvalidOperation(msg)


def _validate_node(node: Any, path: str) -> Dict[str, Any]:
    _require(isinstance(node, dict), f"{path} must be an object")
    for key in ("id", "operation", "adapter_id"):
        val = node.get(key)
        _require(isinstance(val, str) and val, f"{path}.{key} must be a non-empty string")
    depends_on = node.get("depends_on", [])
    _require(isinstance(depends_on, list) and all(isinstance(d, str) and d for d in depends_on),
              f"{path}.depends_on must be a list of non-empty strings")
    input_assets = node.get("input_assets", [])
    _require(isinstance(input_assets, list) and all(isinstance(a, str) for a in input_assets),
              f"{path}.input_assets must be a list of strings")
    input_documents = node.get("input_documents", [])
    _require(isinstance(input_documents, list), f"{path}.input_documents must be a list")
    for i, doc_ref in enumerate(input_documents):
        _require(isinstance(doc_ref, dict) and isinstance(doc_ref.get("document_id"), str)
                  and doc_ref.get("document_id") and isinstance(doc_ref.get("revision"), int),
                  f"{path}.input_documents[{i}] must be {{document_id, revision}}")
    parameters = node.get("parameters", {})
    _require(isinstance(parameters, dict), f"{path}.parameters must be an object")
    external_effect = node.get("external_effect", True)
    _require(isinstance(external_effect, bool), f"{path}.external_effect must be a bool")
    retry_policy = node.get("retry_policy", "before_effect_only")
    _require(retry_policy in RETRY_POLICIES, f"{path}.retry_policy must be one of {RETRY_POLICIES}")
    return {
        "id": node["id"], "depends_on": list(depends_on), "operation": node["operation"],
        "adapter_id": node["adapter_id"], "input_assets": list(input_assets),
        "input_documents": [dict(d) for d in input_documents], "parameters": dict(parameters),
        "external_effect": external_effect, "retry_policy": retry_policy,
    }


def _validate_criterion(criterion: Any, path: str) -> Dict[str, Any]:
    _require(isinstance(criterion, dict), f"{path} must be an object")
    _require(isinstance(criterion.get("id"), str) and criterion["id"], f"{path}.id must be a non-empty string")
    _require(criterion.get("kind") in CRITERION_KINDS, f"{path}.kind must be one of {CRITERION_KINDS}")
    _require(isinstance(criterion.get("target_node"), str) and criterion["target_node"],
              f"{path}.target_node must be a non-empty string")
    state = criterion.get("state", "pending")
    _require(state in CRITERION_STATES, f"{path}.state must be one of {CRITERION_STATES}")
    evidence_refs = criterion.get("evidence_refs", [])
    _require(isinstance(evidence_refs, list), f"{path}.evidence_refs must be a list")
    return {"id": criterion["id"], "kind": criterion["kind"], "target_node": criterion["target_node"],
            "state": state, "evidence_refs": list(evidence_refs)}


def _find_cycle(nodes: Sequence[Dict[str, Any]]) -> Optional[List[str]]:
    by_id = {n["id"]: n for n in nodes}
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {n["id"]: WHITE for n in nodes}
    path: List[str] = []

    def visit(node_id: str) -> Optional[List[str]]:
        color[node_id] = GRAY
        path.append(node_id)
        for dep in by_id[node_id]["depends_on"]:
            if color.get(dep) == GRAY:
                idx = path.index(dep)
                return path[idx:] + [dep]
            if color.get(dep) == WHITE:
                found = visit(dep)
                if found:
                    return found
        path.pop()
        color[node_id] = BLACK
        return None

    for node_id in by_id:
        if color[node_id] == WHITE:
            found = visit(node_id)
            if found:
                return found
    return None


def validate(raw: Mapping[str, Any], *, owner: str = "", project_id: str = "",
             plan_id: str = "", revision: int = 1) -> ProductionPlan:
    """Structural + DAG validation. Raises :class:`InvalidOperation` naming
    the exact problem (dangling ``depends_on``, a cycle, an unknown
    ``retry_policy``/criterion ``kind``, a duplicate node id) — never a
    silent best-effort acceptance."""
    _require(isinstance(raw, Mapping), "plan must be an object")
    brief = raw.get("brief")
    _require(isinstance(brief, dict) and isinstance(brief.get("document_id"), str)
              and brief.get("document_id") and isinstance(brief.get("revision"), int),
              "plan.brief must be {document_id, revision}")

    raw_nodes = raw.get("nodes")
    _require(isinstance(raw_nodes, list) and len(raw_nodes) >= 1, "plan.nodes must be a non-empty list")
    nodes = [_validate_node(n, f"plan.nodes[{i}]") for i, n in enumerate(raw_nodes)]
    ids = [n["id"] for n in nodes]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    _require(not dupes, f"plan.nodes has duplicate ids: {dupes}")
    known = set(ids)
    for node in nodes:
        unknown = sorted(set(node["depends_on"]) - known)
        _require(not unknown, f"plan.nodes[{node['id']}].depends_on names unknown step(s): {unknown}")
        _require(node["id"] not in node["depends_on"], f"plan.nodes[{node['id']}] cannot depend on itself")
    cycle = _find_cycle(nodes)
    _require(cycle is None, f"plan.nodes has a dependency cycle: {' -> '.join(cycle or [])}")

    raw_criteria = raw.get("criteria", [])
    _require(isinstance(raw_criteria, list), "plan.criteria must be a list")
    criteria = [_validate_criterion(c, f"plan.criteria[{i}]") for i, c in enumerate(raw_criteria)]
    for c in criteria:
        _require(c["target_node"] in known, f"plan.criteria[{c['id']}].target_node names an unknown step")

    budget = raw.get("budget", {}) or {}
    _require(isinstance(budget, dict), "plan.budget must be an object")

    available_assets = raw.get("available_assets", [])
    _require(isinstance(available_assets, list) and all(isinstance(a, str) for a in available_assets),
              "plan.available_assets must be a list of strings")

    approval = raw.get("approval") or {"state": "required", "plan_digest": ""}
    _require(isinstance(approval, dict), "plan.approval must be an object")

    now = time.time()
    return ProductionPlan(
        schema_version=1, id=str(raw.get("id") or plan_id or ""),
        project_id=str(raw.get("project_id") or project_id or ""), owner=str(owner or ""),
        revision=int(raw.get("revision") or revision),
        brief=dict(brief), available_assets=tuple(available_assets), nodes=tuple(nodes),
        criteria=tuple(criteria), budget=dict(budget), approval=dict(approval),
        execution_requested=bool(raw.get("execution_requested", False)),
        created_at=float(raw.get("created_at") or now), updated_at=now,
    )


# ── persistence ─────────────────────────────────────────────────────────

def default_path() -> str:
    from src.constants import DATA_DIR
    return os.path.join(DATA_DIR, "creator", "production_plans.sqlite3")


class ProductionPlanStore:
    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = db_path or default_path()
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        self._init_lock = threading.Lock()
        self._ensure_schema()

    @contextmanager
    def _conn(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=_BUSY_TIMEOUT_S, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            if immediate:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            if immediate:
                conn.execute("COMMIT")
        except Exception:
            if immediate:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        with self._init_lock, self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS production_plans (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    plan_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_production_plans_owner_project "
                "ON production_plans(owner, project_id)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS productions (
                    id TEXT PRIMARY KEY,
                    plan_id TEXT NOT NULL,
                    plan_revision INTEGER NOT NULL,
                    owner TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    workflow_run_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_productions_idem "
                "ON productions(plan_id, idempotency_key)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_productions_owner "
                "ON productions(owner, project_id)"
            )

    # -- plans --------------------------------------------------------

    def save_plan(self, plan: ProductionPlan) -> None:
        stamp = time.time()
        with self._conn(immediate=True) as conn:
            conn.execute(
                "INSERT INTO production_plans (id, project_id, owner, revision, plan_json, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (plan.id, plan.project_id, plan.owner, plan.revision,
                 json.dumps(plan.to_public_dict()), plan.created_at, stamp),
            )

    def get_plan(self, owner: str, plan_id: str) -> Optional[ProductionPlan]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM production_plans WHERE id = ? AND owner = ?",
                (plan_id, owner or ""),
            ).fetchone()
            if row is None:
                return None
            data = json.loads(row["plan_json"])
            data["created_at"] = row["created_at"]
            return validate(data, owner=owner, project_id=row["project_id"],
                             plan_id=row["id"], revision=row["revision"])

    # -- productions ----------------------------------------------------

    def create_production(self, *, production_id: str, plan_id: str, plan_revision: int,
                           owner: str, project_id: str, workflow_run_id: str,
                           idempotency_key: str) -> Dict[str, Any]:
        stamp = time.time()
        with self._conn(immediate=True) as conn:
            existing = conn.execute(
                "SELECT * FROM productions WHERE plan_id = ? AND idempotency_key = ?",
                (plan_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                return dict(existing)
            conn.execute(
                "INSERT INTO productions (id, plan_id, plan_revision, owner, project_id, "
                "workflow_run_id, idempotency_key, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (production_id, plan_id, plan_revision, owner or "", project_id,
                 workflow_run_id, idempotency_key, stamp, stamp),
            )
            row = conn.execute("SELECT * FROM productions WHERE id = ?", (production_id,)).fetchone()
            return dict(row)

    def get_production(self, owner: str, production_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM productions WHERE id = ? AND owner = ?",
                (production_id, owner or ""),
            ).fetchone()
            return dict(row) if row is not None else None


_store: Optional[ProductionPlanStore] = None
_store_lock = threading.Lock()


def get_store() -> ProductionPlanStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = ProductionPlanStore()
    return _store


# ── create ───────────────────────────────────────────────────────────

def create(store: ProductionPlanStore, owner: str, project_id: str, raw: Mapping[str, Any]) -> ProductionPlan:
    plan_id = f"plan_{uuid.uuid4().hex[:20]}"
    payload = dict(raw)
    payload["id"] = plan_id
    payload["project_id"] = project_id
    payload["revision"] = 1
    plan = validate(payload, owner=owner, project_id=project_id, plan_id=plan_id, revision=1)
    store.save_plan(plan)
    return plan


def get(store: ProductionPlanStore, owner: str, plan_id: str) -> ProductionPlan:
    plan = store.get_plan(owner, plan_id)
    if plan is None:
        raise PlanNotFound(plan_id)
    return plan


# ── estimate (pure — no side effects, WP09 reused) ─────────────────────

def _adapter_engine_and_deployment(node: Dict[str, Any]) -> Tuple[str, str]:
    adapter_id = node["adapter_id"]
    return adapter_id, adapter_id


def estimate(plan: ProductionPlan, *, profile: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Aggregates ``preflight.run_preflight`` per step — no adapter is ever
    called, no budget reserved (WP09 discipline, reused verbatim). Cost is
    ``"unknown"`` the instant any step's cost is, never a partial sum."""
    from .preflight import run_preflight

    per_node: List[Dict[str, Any]] = []
    all_missing: List[Dict[str, Any]] = []
    total_tokens: Optional[int] = 0
    any_unknown_tokens = False
    any_requires_approval = False
    for node in plan.nodes:
        engine, deployment_id = _adapter_engine_and_deployment(node)
        report = run_preflight(
            owner=plan.owner, project_id=plan.project_id, operation=node["operation"],
            engine=engine, deployment_id=deployment_id, params=node["parameters"],
            inputs=node["input_assets"], profile=profile,
        )
        per_node.append({"step_id": node["id"], "preflight": report.to_dict()})
        for m in report.missing:
            all_missing.append({**dict(m), "step_id": node["id"]})
        if report.estimate.tokens is None:
            any_unknown_tokens = True
        elif total_tokens is not None:
            total_tokens += report.estimate.tokens
        any_requires_approval = any_requires_approval or report.requires_approval

    approval_digest: Optional[str] = None
    approval_plan_dict: Optional[Dict[str, Any]] = None
    if any_requires_approval:
        from .preflight import build_approval_plan, Estimate

        agg_estimate = Estimate(tokens=None if any_unknown_tokens else total_tokens)
        plan_obj = build_approval_plan(
            operation="creator.production_plan.execute", engine="multiple",
            deployment_id=plan.id, project_id=plan.project_id,
            inputs=[a for n in plan.nodes for a in n["input_assets"]],
            params={"plan_id": plan.id, "plan_revision": plan.revision,
                    "steps": [n["id"] for n in plan.nodes]},
            estimate=agg_estimate, action="cost_over_budget",
            detail=f"production plan {plan.id} revision {plan.revision}: "
                   f"{len(plan.nodes)} step(s) need approval before execute()",
        )
        approval_digest = plan_obj.fingerprint()
        approval_plan_dict = plan_obj.to_dict()

    return {
        "ok": not all_missing,
        "missing": all_missing,
        "steps": per_node,
        "total_tokens": None if any_unknown_tokens else total_tokens,
        "requires_approval": any_requires_approval,
        "approval_digest": approval_digest,
        "approval_plan": approval_plan_dict,
    }


# ── compile_to_workflow ─────────────────────────────────────────────────

def _skill_id(plan_id: str, step_id: str) -> str:
    return f"creator_plan.{plan_id}.{step_id}"


def compile_to_workflow(plan: ProductionPlan):
    """Turns ``plan.nodes`` into a real ``WorkflowDefinition``: one
    effectful ``skill`` node per step, ``needs`` mirroring ``depends_on``.
    ``config.creator_plan_step`` is the marker ``creator_step_handler`` (and
    the ``runtime.py`` hook that resumes a plan step even from the
    background scheduler) key off."""
    from src.contracts import WorkflowDefinition

    nodes = []
    for node in plan.nodes:
        nodes.append({
            "id": node["id"], "type": "skill", "title": node["operation"],
            "needs": list(node["depends_on"]),
            "config": {
                "skill": _skill_id(plan.id, node["id"]),
                "creator_plan_step": True,
                "plan_id": plan.id, "plan_revision": plan.revision,
                "step_id": node["id"], "adapter_id": node["adapter_id"],
                "operation": node["operation"], "parameters": node["parameters"],
                "input_assets": node["input_assets"], "retry_policy": node["retry_policy"],
                "external_effect": node["external_effect"],
            },
            "max_attempts": 1,
        })
    return WorkflowDefinition.parse({
        "id": f"creator_production_plan.{plan.id}",
        "version": f"{plan.revision}.0.0", "title": f"Production plan {plan.id}",
        "description": f"Compiled from creator production plan {plan.id} rev {plan.revision}",
        "nodes": nodes,
    })


# ── the step handler (submit + collect, unknown_effect via mark_effect) ─

def _resolve_adapter(adapter_id: str):
    from . import adapters as adapter_registry
    return adapter_registry.get(adapter_id)


def creator_step_handler(node, context: Mapping[str, Any]) -> Dict[str, Any]:
    """One plan step: on the first pass, plan+submit through the adapter and
    pause with a wake time; on the resume pass, ask the adapter for status
    and, once complete, collect + register outputs via
    ``production_runs.link_outputs`` and finish. Mirrors
    ``src/workflows/handlers.py::media_skill_runner``'s pause/resume shape
    (the exact seam this file's docstring points at), but drives WP10's
    ``AdapterPort`` instead of ``media_runs`` directly, because most
    adapters (ffmpeg, fake, future ones) have no ``media_runs`` authority
    of their own — see ``production_runs.py``'s own docstring."""
    from .adapter_port import AdapterPlan, Staging
    from . import production_runs

    config = dict(node.config or {})
    owner = str(context.get("owner") or "")
    project_id = str(context.get("project_id") or "")
    adapter_id = str(config.get("adapter_id") or "")
    operation = str(config.get("operation") or "")
    parameters = dict(config.get("parameters") or {})
    input_assets = list(config.get("input_assets") or [])
    step_id = str(config.get("step_id") or node.id)

    previous = dict(context.get("previous") or {})
    job_id = str(previous.get("job_id") or "")

    try:
        adapter = _resolve_adapter(adapter_id)
    except KeyError as exc:
        return {"status": "failed", "reason": f"unknown adapter {adapter_id!r}: {exc}"}

    if not job_id:
        # ── first pass: plan (pure) then submit (effect) ──────────────
        plan_obj = adapter.plan(operation, parameters, input_assets)
        if not plan_obj.ok:
            return {"status": "failed", "reason": plan_obj.detail or "adapter refused to plan this step",
                     "missing": list(plan_obj.missing)}

        try:
            from .adapters.base import stage_inputs
            staging = stage_inputs(owner=owner, project_id=project_id, occurrence_ids=input_assets)
        except Exception as exc:  # noqa: BLE001 - staging failure is this step's failure, not a crash
            return {"status": "failed", "reason": f"could not stage inputs: {exc}"}

        mark_effect = context.get("mark_effect")
        if callable(mark_effect):
            mark_effect("pending")
        submitted = adapter.submit(plan_obj, staging)
        if callable(mark_effect):
            # The call returned — whatever it did, it finished doing it.
            # A crash between the two mark_effect() calls above/below is
            # exactly what src/workflows/store.py's lease recovery turns
            # into effect_state == "unknown" on the next pass.
            mark_effect("confirmed")

        run_row = production_runs.record_submit(
            adapter_name=adapter_id, engine=adapter_id, op=operation,
            job_id=submitted.job_id, submit_state=submitted.state,
            owner=owner, project_id=project_id, values=parameters,
            reason=submitted.reason,
        )

        if submitted.state == "rejected_before_queue":
            return {"status": "failed", "reason": submitted.reason or "rejected before queue",
                     "detail": submitted.detail, "step_id": step_id}
        if submitted.state == "accepted_uncertain":
            # Honest: we do not know whether the far side got this. Keep
            # asking — status()/reconcile() is how this resolves, never a
            # blind retry of submit().
            return {"status": "paused", "job_id": submitted.job_id, "step_id": step_id,
                     "unknown_submit": True, "wake_at": _wake_in(POLL_SECONDS),
                     "reason": "submit acknowledgement was lost; polling status/reconcile",
                     "media_run": run_row}
        return {"status": "paused", "job_id": submitted.job_id, "step_id": step_id,
                "wake_at": _wake_in(POLL_SECONDS), "reason": f"submitted as {submitted.job_id}",
                "media_run": run_row}

    # ── resume pass: ask the engine, collect once complete ─────────────
    unknown_submit = bool(previous.get("unknown_submit"))
    status = adapter.reconcile(job_id) if unknown_submit else adapter.status(job_id)

    if status.state in ("queued", "running"):
        return {"status": "paused", "job_id": job_id, "step_id": step_id,
                "wake_at": _wake_in(POLL_SECONDS), "reason": f"{status.state}: {status.detail}",
                "unknown_submit": unknown_submit}
    if status.state == "unknown":
        polls = int(previous.get("polls", 0)) + 1
        if polls < 6:
            return {"status": "paused", "job_id": job_id, "step_id": step_id, "polls": polls,
                     "wake_at": _wake_in(POLL_SECONDS),
                     "reason": "engine cannot answer for this job yet (unknown); retrying reconcile",
                     "unknown_submit": True}
        return {"status": "failed", "job_id": job_id, "step_id": step_id, "unknown_effect": True,
                "reason": "reconcile could not resolve this job's outcome after repeated attempts; "
                          "manual reconciliation required"}
    if status.state == "cancelled":
        production_runs.mark_terminal(job_id, adapter_name=adapter_id, status="cancelled",
                                       reason=status.detail)
        return {"status": "failed", "job_id": job_id, "step_id": step_id,
                "reason": f"cancelled: {status.detail}"}
    if status.state == "failed":
        production_runs.mark_terminal(job_id, adapter_name=adapter_id, status="failed",
                                       reason=status.detail)
        return {"status": "failed", "job_id": job_id, "step_id": step_id, "reason": status.detail}

    # completed: collect (bounded local effect) + register outputs.
    from .adapter_port import new_staging_dir

    tmp = new_staging_dir(prefix="faustus-creator-collect-")
    collected = adapter.collect(job_id, tmp)
    settled = production_runs.link_outputs(
        job_id, adapter_name=adapter_id, collected=collected, owner=owner,
        project_id=project_id, op=operation, input_occurrence_ids=input_assets,
    )
    if settled.get("status") != "completed":
        return {"status": "failed", "job_id": job_id, "step_id": step_id,
                "reason": settled.get("reason") or "collect did not produce a valid output"}
    return {"status": "completed", "job_id": job_id, "step_id": step_id,
            "artifact_ids": settled.get("artifact_ids", []), "artifacts": settled.get("artifacts", [])}


def _wake_in(seconds: int) -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(seconds=max(1, int(seconds)))).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── execute ──────────────────────────────────────────────────────────

def _current_input_revisions(plan: ProductionPlan) -> List[Dict[str, Any]]:
    """Every ``{document_id, revision}`` the plan referenced (its brief plus
    each node's ``input_documents``), read fresh right now."""
    refs: List[Tuple[str, int]] = [(plan.brief["document_id"], plan.brief["revision"])]
    for node in plan.nodes:
        for doc_ref in node["input_documents"]:
            refs.append((doc_ref["document_id"], doc_ref["revision"]))
    return [{"document_id": d, "revision": r} for d, r in refs]


def _revalidate_against_current(plan: ProductionPlan) -> List[Dict[str, Any]]:
    """Re-reads every input document this plan depends on and reports which
    ones moved. A document that no longer exists for this owner is reported
    as changed too (``current_revision: None``) — never silently ignored."""
    from .store import get_store as get_document_store

    doc_store = get_document_store()
    changed: List[Dict[str, Any]] = []
    seen = set()
    for ref in _current_input_revisions(plan):
        doc_id = ref["document_id"]
        if doc_id in seen:
            continue
        seen.add(doc_id)
        current = doc_store.get(plan.owner, doc_id)
        current_revision = current.revision if current is not None else None
        if current_revision != ref["revision"]:
            changed.append({"document_id": doc_id, "plan_revision": ref["revision"],
                              "current_revision": current_revision})
    return changed


def _consume_approval(plan: ProductionPlan, est: Dict[str, Any], approval_digest: Optional[str],
                       *, owner: str) -> None:
    if not est.get("requires_approval"):
        return
    if not approval_digest or approval_digest != est.get("approval_digest"):
        raise InvalidOperation(
            "this plan needs an approved digest to execute; call estimate() first and "
            "approve exactly the digest it returns"
        )
    from src import approval_store
    from src.contracts import ApprovalPlan
    from core.database import ApprovalRow, SessionLocal

    plan_obj = ApprovalPlan.parse(est["approval_plan"])
    db = SessionLocal()
    try:
        row = (db.query(ApprovalRow)
               .filter(ApprovalRow.plan_fingerprint == plan_obj.fingerprint(),
                       ApprovalRow.owner == (owner or None))
               .order_by(ApprovalRow.decided_at.desc()).first())
        approval_id = row.id if row is not None else ""
    finally:
        db.close()
    if not approval_id:
        raise InvalidOperation("no approval was ever granted for this exact plan digest")
    spent = approval_store.consume(approval_id, plan_obj, owner=owner)
    if not spent.get("ok"):
        raise InvalidOperation(
            f"approval could not be consumed ({spent.get('reason', 'consume_failed')}): "
            "already spent, or the plan changed since it was granted"
        )


def execute(store: ProductionPlanStore, plan_id: str, *, owner: str, idempotency_key: str,
            approval_digest: Optional[str] = None,
            profile: Optional[Mapping[str, Any]] = None,
            max_sync_passes: int = MAX_SYNC_PASSES) -> Dict[str, Any]:
    """Same ``idempotency_key`` (scoped to this plan) → the same production,
    every time — even across a retried HTTP call. Re-validates the plan
    against CURRENT input document revisions before doing anything else, so
    a brief/storyboard edited after the plan was built is refused (raises
    :class:`PlanRevisionConflict`) rather than executed against stale
    intent."""
    if not idempotency_key:
        raise InvalidOperation("idempotency_key is required")

    plan = get(store, owner, plan_id)

    changed = _revalidate_against_current(plan)
    if changed:
        raise PlanRevisionConflict(changed)

    est = estimate(plan, profile=profile)
    _consume_approval(plan, est, approval_digest, owner=owner)

    definition = compile_to_workflow(plan)
    from src.workflows.store import WorkflowStore
    from src.workflows.engine import WorkflowEngine

    wf_store = WorkflowStore()
    created = wf_store.create_run(
        definition, owner=owner, project_id=plan.project_id, trigger="manual",
        inputs={"plan_id": plan.id, "plan_revision": plan.revision},
        dedupe_key=f"creator_plan:{plan.id}:{idempotency_key}",
    )
    run_id = created["run_id"]

    production_id = f"prod_{uuid.uuid4().hex[:20]}"
    production_row = store.create_production(
        production_id=production_id, plan_id=plan.id, plan_revision=plan.revision,
        owner=owner, project_id=plan.project_id, workflow_run_id=run_id,
        idempotency_key=idempotency_key,
    )

    engine = WorkflowEngine({"skill": creator_step_handler}, wf_store)
    passes = 0
    result: Dict[str, Any] = {}
    while passes < max_sync_passes:
        result = engine.advance(run_id, max_nodes=len(plan.nodes) or 1)
        passes += 1
        status = result.get("status")
        if status in ("completed", "failed", "cancelled"):
            break
        if result.get("reason") in ("paused",):
            # Nothing is due yet (a real render still running) — stop
            # synchronous driving; the background scheduler or a later
            # production_status()/execute() retry picks this up.
            break
    else:
        pass

    return production_status(store, production_row["id"], owner=owner)


# ── cancel ───────────────────────────────────────────────────────────

def cancel(store: ProductionPlanStore, production_id: str, *, owner: str) -> Dict[str, Any]:
    """Stops every not-yet-started step and reconciles every step whose
    node had already reached a real ``job_id`` — the
    ``requested|accepted|confirmed|too_late|unknown`` vocabulary
    ``adapter_port.CancelResult`` already defines, never re-derived here."""
    row = store.get_production(owner, production_id)
    if row is None:
        raise ProductionNotFound(production_id)

    from src.workflows.store import WorkflowStore

    wf_store = WorkflowStore()
    run_id = row["workflow_run_id"]
    loaded = wf_store.get_run(run_id)
    if loaded is None:
        raise ProductionNotFound(production_id)

    outcomes: List[Dict[str, Any]] = []
    if loaded["run"].status not in ("completed", "failed", "cancelled"):
        wf_store.set_run_status(run_id, "cancelled", reason="cancelled by production_plan.cancel()")

    states = wf_store.node_runs(run_id)
    by_id = {n.id: n for n in loaded["definition"].nodes}
    for node_id, node in by_id.items():
        state = states.get(node_id)
        config = dict(node.config or {})
        adapter_id = str(config.get("adapter_id") or "")
        job_id = str((state.result or {}).get("job_id") or "") if state else ""
        if state is None:
            # Never even claimed — nothing to reconcile, just stopped.
            outcomes.append({"step_id": node_id, "outcome": "requested", "job_id": ""})
            continue
        if state.status in ("completed", "failed", "cancelled"):
            outcomes.append({"step_id": node_id, "outcome": "confirmed" if state.status == "completed" else state.status,
                              "job_id": job_id})
            continue
        if not job_id:
            # Claimed (e.g. blocked) but never reached a real job — nothing
            # to reconcile, just stopped.
            outcomes.append({"step_id": node_id, "outcome": "requested", "job_id": ""})
            continue
        try:
            adapter = _resolve_adapter(adapter_id)
            cancel_result = adapter.cancel(job_id)
        except Exception as exc:  # noqa: BLE001 - a cancel failure is still an outcome, not a crash
            outcomes.append({"step_id": node_id, "outcome": "unknown", "job_id": job_id, "detail": str(exc)})
            continue
        from . import production_runs
        if cancel_result.outcome == "confirmed":
            production_runs.mark_terminal(job_id, adapter_name=adapter_id, status="cancelled",
                                           reason="cancelled")
        wf_store.finish_node(run_id, node_id, status="cancelled",
                              result={**dict(state.result or {}), "cancel_outcome": cancel_result.outcome})
        outcomes.append({"step_id": node_id, "outcome": cancel_result.outcome, "job_id": job_id,
                          "detail": cancel_result.detail})

    return {"production_id": production_id, "status": "cancelled", "steps": outcomes}


# ── production_status (projection only) ─────────────────────────────────

def production_status(store: ProductionPlanStore, production_id: str, *, owner: str) -> Dict[str, Any]:
    row = store.get_production(owner, production_id)
    if row is None:
        raise ProductionNotFound(production_id)

    from src.workflows.store import WorkflowStore

    wf_store = WorkflowStore()
    run_id = row["workflow_run_id"]
    loaded = wf_store.get_run(run_id)
    if loaded is None:
        raise ProductionNotFound(production_id)

    states = wf_store.node_runs(run_id)
    steps: List[Dict[str, Any]] = []
    for node in loaded["definition"].nodes:
        state = states.get(node.id)
        result = dict(state.result) if state else {}
        job_id = str(result.get("job_id") or "")
        media_run: Optional[Dict[str, Any]] = None
        if job_id:
            from src import media_runs
            media_run = media_runs.get(job_id)
        steps.append({
            "step_id": node.id, "status": state.status if state else "pending",
            "job_id": job_id, "reason": state.reason if state else "",
            "artifact_ids": result.get("artifact_ids", []),
            "media_run": media_run,
        })

    return {
        "production_id": production_id, "plan_id": row["plan_id"],
        "plan_revision": row["plan_revision"], "workflow_run_id": run_id,
        "status": loaded["run"].status, "steps": steps,
    }
