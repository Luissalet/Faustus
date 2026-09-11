"""
workflows/preflight.py — what running a definition would need, before anyone
authorizes it.

ADP-16 (aigraphstudio §6.2, "dry-run de recursos"): enumerate connections,
tools, permissions, human waits, inputs, outputs and a token/cost estimate
for one `src.contracts.workflow.WorkflowDefinition`, using only fields that
definition actually declares — no LLM call, no script, no download, no
network of any kind. `src/workflow_cost_estimate.py` already does the hard
part of this (calls_min/calls_max under `condition` gating and unbounded
cycles); this module reuses it rather than re-deriving the same graph walk,
and adds the parts ADP-16 asks for beyond price: declared tools, permissions,
human waits, entry points and destinations, and `agent_profile_lint
.lint_workflow`'s findings as `warnings`.

Two things this module refuses to blur, because ADP-16's own limits section
names them explicitly:

* **Scenario vs conservative are different numbers, never one "expected"
  value.** A `condition` node might skip a branch; a cycle (only reachable on
  a hand-built definition — `.parse()` already refuses one) has no bound this
  schema records. "Scenario" is the cheapest path a `.parse()`d definition
  can take; "conservative" is the most expensive one `workflow_cost_estimate`
  can compute. Neither is presented as a probability.

* **A cost that cannot be priced is `"unknown"`, not folded into a partial
  total that looks complete.** If any `skill` node that would actually run
  has no price — either because it names no `config.model`, or because
  `prices` does not cover the model it names — `cost.scenario`/
  `cost.conservative` are the string `"unknown"` rather than a number that
  silently excludes that node's spend. `cost.unpriced` names every such
  node/model, and `cost.per_node` keeps the per-node breakdown so the
  per-token price of what IS priced stays visible even while the run total
  reads `"unknown"`.

* **Per-token price and run budget are different fields.** `cost.per_node`
  rows carry a USD-per-call number; `cost.scenario`/`cost.conservative` are a
  whole-run guess. Nothing here lets a per-token figure be read as if it were
  a cap on total spend — see `docs/api/topology.md`, which repeats this for
  the same reason `workflow_cost_estimate.py`'s own docstring does.

What this module does NOT attempt, documented rather than guessed:
`WorkflowNode.config` is a free-form mapping (rule: nothing is invented that
the schema does not declare), and there is no formal "declared inputs" field
on a definition — inputs arrive at `POST /api/workflows/runs` as free-form
JSON, never validated against the definition. So `inputs` below is read the
same defensive way `src/workflows/handlers.py::resolve` already reads a run's
inputs: `inputs.<key>` paths named in a `condition` node's `config.when` —
the one place this schema expresses "read from the run's inputs" as a
structured path rather than free text a handler interprets on its own. A
workflow that only reaches an input through a `skill`/`deliver` node's opaque
config is not represented here — that gap is real, not hidden.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from src.agent_profile_lint import lint_workflow
from src.contracts.workflow import WorkflowDefinition, WorkflowNode
from src.workflow_cost_estimate import ModelPrice, estimate as _cost_estimate

__all__ = ["Preflight", "preflight"]


def _as_workflow_definition(definition: Any) -> WorkflowDefinition:
    if isinstance(definition, WorkflowDefinition):
        return definition
    if isinstance(definition, Mapping):
        return WorkflowDefinition.parse(definition)
    raise TypeError("preflight expects a WorkflowDefinition or a mapping")


@dataclass(frozen=True)
class Preflight:
    """Everything ADP-16 asks a dry-run to surface before a plan is
    authorized. See the module docstring for why `token_estimate` and `cost`
    are separate dicts, and why `cost.per_node` (per-token price) and
    `cost.scenario`/`cost.conservative` (a run budget guess) are separate
    fields inside `cost` itself."""

    connections: Tuple[str, ...]
    tools: Tuple[str, ...]
    permissions_required: Tuple[str, ...]
    inputs: Tuple[str, ...]
    outputs: Tuple[Dict[str, Any], ...]
    human_waits: Tuple[str, ...]
    token_estimate: Dict[str, Any]
    cost: Dict[str, Any]
    warnings: Tuple[Dict[str, Any], ...]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "connections": list(self.connections),
            "tools": list(self.tools),
            "permissions_required": list(self.permissions_required),
            "inputs": list(self.inputs),
            "outputs": [dict(row) for row in self.outputs],
            "human_waits": list(self.human_waits),
            "token_estimate": dict(self.token_estimate),
            "cost": dict(self.cost),
            "warnings": [dict(row) for row in self.warnings],
        }


def _referenced_input_keys(nodes: Sequence[WorkflowNode]) -> List[str]:
    """Best-effort: `inputs.<key>` paths named in a `condition` node's
    `config.when.left`/`right`. See the module docstring for the scope
    limit — a `skill`/`deliver` node's opaque `config` is not scanned."""
    found: set = set()
    for node in nodes:
        if node.type != "condition":
            continue
        when = node.config.get("when") if isinstance(node.config, Mapping) else None
        if not isinstance(when, Mapping):
            continue
        for side in (when.get("left"), when.get("right")):
            if isinstance(side, Mapping):
                path = side.get("path")
                if isinstance(path, str) and path.startswith("inputs."):
                    found.add(path)
    return sorted(found)


def _permissions(nodes: Sequence[WorkflowNode]) -> List[str]:
    """Union of `config.permissions` across every node — read the same
    defensive way `src/workflows/handlers.py::_approval_plan` reads it (a
    convention, never enforced by `WorkflowNode`)."""
    found: set = set()
    for node in nodes:
        perms = node.config.get("permissions") if isinstance(node.config, Mapping) else None
        if isinstance(perms, (list, tuple)):
            found.update(str(p) for p in perms if isinstance(p, str) and p.strip())
    return sorted(found)


def preflight(
    definition: Any,
    *,
    installed_models: Optional[Sequence[str]] = None,
    prices: Optional[Mapping[str, ModelPrice]] = None,
    default_tokens_per_call: Tuple[int, int] = (1500, 500),
    assumed_iterations: int = 3,
) -> Preflight:
    """A dry-run report for one run of `definition`.

    Pure and read-only, the same guarantee `workflow_cost_estimate.estimate`
    and `agent_profile_lint.lint_workflow` already make: no LLM call, no
    script, no download, no network — this function only reads the
    definition and calls those two, both themselves pure. `definition` may
    already be a `WorkflowDefinition` or a raw dict in the shape `POST
    /api/workflows/validate` accepts.

    `installed_models`, when given, only narrows whether a named model is
    reported as locally installed in `token_estimate.per_node` — it never
    changes what runs or what is priced; omitted/`None` means "not checked",
    not "none installed".
    """
    wf = _as_workflow_definition(definition)

    cost_estimate = _cost_estimate(
        wf, prices=prices, default_tokens_per_call=default_tokens_per_call,
        assumed_iterations=assumed_iterations,
    )

    prompt_tokens, completion_tokens = default_tokens_per_call
    tokens_per_call = prompt_tokens + completion_tokens
    token_rows: List[Dict[str, Any]] = []
    tokens_min_total = tokens_max_total = 0
    unpriced: set = set(cost_estimate.unpriced_models)
    for row in cost_estimate.per_node:
        if row["type"] != "skill":
            continue
        installed = None
        if installed_models is not None and row["model"]:
            installed = row["model"] in set(installed_models)
        token_rows.append({
            "node_id": row["node_id"], "model": row["model"] or "unknown",
            "tokens_min": row["calls_min"] * tokens_per_call,
            "tokens_max": row["calls_max"] * tokens_per_call,
            "model_installed_locally": installed,
        })
        tokens_min_total += row["calls_min"] * tokens_per_call
        tokens_max_total += row["calls_max"] * tokens_per_call
        # `workflow_cost_estimate.estimate` only lists a model in
        # `unpriced_models` when one IS named but missing from `prices`; a
        # `skill` node with no `config.model` at all is a second, silent
        # unknown ("no config.model — cost unknown, excluded from totals" in
        # its own `note`) that this module refuses to let disappear from
        # `cost.unpriced` the same way (rule: unknown price is never 0).
        if not row["model"]:
            unpriced.add(f"node:{row['node_id']} (no config.model)")

    incomplete_cost = bool(unpriced)
    cost: Dict[str, Any] = {
        "scenario": "unknown" if incomplete_cost else cost_estimate.total_usd_min,
        "conservative": "unknown" if incomplete_cost else cost_estimate.total_usd_max,
        "unpriced": sorted(unpriced),
        "per_node": [dict(row) for row in cost_estimate.per_node],
        "basis": "cost.per_node is a per-call USD price from the caller's `prices` "
                 "mapping; cost.scenario/conservative is a whole-run budget guess. "
                 "They are separate fields on purpose (ADP-16) — a per-token price "
                 "is never a cap on total spend.",
    }

    connections: set = set()
    tools: List[str] = []
    seen_tools: set = set()
    outputs: List[Dict[str, Any]] = []
    human_waits: List[str] = []

    for node in wf.nodes:
        config = node.config if isinstance(node.config, Mapping) else {}
        if node.type == "skill":
            skill_id = str(config.get("skill") or "")
            if skill_id:
                if skill_id not in seen_tools:
                    seen_tools.add(skill_id)
                    tools.append(skill_id)
                connections.add("media" if skill_id.startswith("media:") else f"skill:{skill_id}")
        elif node.type == "deliver":
            backend = str(config.get("backend") or "")
            connections.add(f"deliver:{backend}" if backend else "deliver:unknown")
        elif node.type == "human_approval":
            human_waits.append(node.id)
        if node.type in ("deliver", "artifact_store"):
            outputs.append({"node_id": node.id, "type": node.type,
                            "title": node.title or node.id})

    warnings = [f.to_dict() for f in lint_workflow(wf)]

    return Preflight(
        connections=tuple(sorted(connections)),
        tools=tuple(tools),
        permissions_required=tuple(_permissions(wf.nodes)),
        inputs=tuple(_referenced_input_keys(wf.nodes)),
        outputs=tuple(outputs),
        human_waits=tuple(human_waits),
        token_estimate={
            "scenario_tokens": tokens_min_total,
            "conservative_tokens": tokens_max_total,
            "per_node": token_rows,
            "basis": f"assumes {prompt_tokens} prompt + {completion_tokens} completion "
                     "tokens per call (same default as workflow_cost_estimate.estimate) "
                     "— a fixed assumption, not a measurement of anything that ran.",
        },
        cost=cost,
        warnings=tuple(warnings),
    )
