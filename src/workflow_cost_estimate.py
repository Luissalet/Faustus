"""workflow_cost_estimate.py — what running a workflow definition would cost.

Lote A4 (aigraphstudio): "estimador de coste = ejecuciones × iteraciones de
bucle × precio por modelo", applied to real
`src.contracts.workflow.WorkflowDefinition` data instead of a separate
drawing-only model.

Three real signals this module reads, all already in the contract:

* **Which nodes can invoke a model at all.** Only a `skill` node runs
  anything that could call an LLM (`src/workflows/handlers.py::skill_handler`
  runs a skill or a media template); every other node type — `manual`,
  `schedule`, `webhook`, `condition`, `wait`, `human_approval`,
  `artifact_store`, `deliver` — is structural or deterministic and is priced
  at $0. A `skill` node optionally names its model at `config.model`; this is
  a convention this module reads defensively (`.get`, never required), not a
  field `WorkflowNode` enforces.

* **Whether a node is conditionally skippable.** `condition_handler`'s own
  docstring is explicit: a failed condition marks the node `skipped`, which
  stops the branch under it (`_stopping` in the engine). So a node with a
  `condition` node anywhere upstream in its `needs` chain MIGHT not run —
  contributing 0 to the cheapest case and 1 to the most expensive one. A node
  with no such ancestor always runs once.

* **Whether a node sits in a cycle.** `WorkflowDefinition.parse()` rejects a
  cycle outright, and no field anywhere in this schema bounds how many times
  one would repeat (no `max_iterations`, no loop-node type — see
  `src/agent_profile_lint.py`'s docstring for the same search). So a cycle
  found on a hand-assembled definition (bypassing `.parse()`) is treated as
  fully unbounded: it contributes an `assumed_iterations` guess, reported
  back by name in `unbounded_loops` rather than silently priced as if it
  were known.

Pricing itself is intentionally NOT looked up from a catalogue this module
invents: `src/model_capabilities.py` and
`src/model_capability_readers/openrouter.py` do not expose a `pricing` field
today (checked before writing this module — the OpenRouter reader keeps the
provider's raw payload on `ModelCapabilityRecord.raw`, which MAY carry
`pricing` once populated from a live catalog fetch, but nothing in this
codebase indexes it by model id yet). So `prices` is read from the caller,
and any `skill` node naming a model this mapping does not cover is reported
in `unpriced_models` with $0 contributed, rather than guessed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

from src.agent_profile_lint import find_cycles
from src.contracts.workflow import WorkflowDefinition, WorkflowNode

__all__ = ["ModelPrice", "Estimate", "estimate"]

#: Every cycle found is treated as unbounded (see module docstring); this is
#: the number of passes assumed through it when no field says otherwise.
DEFAULT_ASSUMED_ITERATIONS = 3


@dataclass(frozen=True)
class ModelPrice:
    """USD per 1,000 tokens, prompt and completion priced separately — the
    same split OpenRouter and every OpenAI-compatible pricing table use."""

    prompt_usd_per_1k: float
    completion_usd_per_1k: float


@dataclass(frozen=True)
class Estimate:
    total_usd_min: float
    total_usd_max: float
    calls_min: int
    calls_max: int
    per_node: Tuple[Dict[str, Any], ...] = ()
    unbounded_loops: Tuple[Dict[str, Any], ...] = ()
    unpriced_models: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_usd_min": self.total_usd_min, "total_usd_max": self.total_usd_max,
            "calls_min": self.calls_min, "calls_max": self.calls_max,
            "per_node": [dict(row) for row in self.per_node],
            "unbounded_loops": [dict(row) for row in self.unbounded_loops],
            "unpriced_models": list(self.unpriced_models),
        }


def _as_workflow_definition(definition: Any) -> WorkflowDefinition:
    if isinstance(definition, WorkflowDefinition):
        return definition
    if isinstance(definition, Mapping):
        return WorkflowDefinition.parse(definition)
    raise TypeError("estimate expects a WorkflowDefinition or a mapping")


def _ancestors(by_id: Mapping[str, WorkflowNode], node_id: str) -> set:
    seen: set = set()
    stack = list(by_id[node_id].needs) if node_id in by_id else []
    while stack:
        current = stack.pop()
        if current in seen or current not in by_id:
            continue
        seen.add(current)
        stack.extend(by_id[current].needs)
    return seen


def estimate(
    definition: Any,
    *,
    prices: Optional[Mapping[str, ModelPrice]] = None,
    default_tokens_per_call: Tuple[int, int] = (1500, 500),
    assumed_iterations: int = DEFAULT_ASSUMED_ITERATIONS,
) -> Estimate:
    """A cost estimate for one run of `definition`.

    Pure and read-only: no network, no lookup outside `prices` and the
    definition itself. `definition` may already be a `WorkflowDefinition` or
    a raw dict in the shape `POST /api/workflows/validate` accepts.
    """
    wf = _as_workflow_definition(definition)
    by_id = {n.id: n for n in wf.nodes}
    prompt_tokens, completion_tokens = default_tokens_per_call
    prices = prices or {}

    cycle_members: Dict[str, int] = {}
    unbounded_loops: List[Dict[str, Any]] = []
    for cycle in find_cycles(wf.nodes):
        unbounded_loops.append({"nodes": list(cycle), "assumed_iterations": assumed_iterations})
        for node_id in cycle:
            cycle_members[node_id] = assumed_iterations

    gated_ids = {
        node.id for node in wf.nodes
        if any(by_id[a].type == "condition" for a in _ancestors(by_id, node.id))
    }

    per_node: List[Dict[str, Any]] = []
    unpriced: List[str] = []
    seen_unpriced: set = set()
    calls_min_total = calls_max_total = 0
    usd_min_total = usd_max_total = 0.0

    for node in wf.nodes:
        if node.id in cycle_members:
            calls_min, calls_max = 1, max(1, cycle_members[node.id])
        elif node.id in gated_ids:
            calls_min, calls_max = 0, 1
        else:
            calls_min, calls_max = 1, 1

        model = ""
        usd_min = usd_max = 0.0
        note = "not a model-invoking node type"
        if node.type == "skill":
            model = str((node.config or {}).get("model") or "").strip()
            if not model:
                note = "no config.model — cost unknown, excluded from totals"
            else:
                price = prices.get(model)
                if price is None:
                    note = f"model '{model}' has no price in `prices` — excluded from totals"
                    if model not in seen_unpriced:
                        seen_unpriced.add(model)
                        unpriced.append(model)
                else:
                    per_call = ((prompt_tokens / 1000.0) * price.prompt_usd_per_1k
                                + (completion_tokens / 1000.0) * price.completion_usd_per_1k)
                    usd_min = per_call * calls_min
                    usd_max = per_call * calls_max
                    note = ""

        per_node.append({
            "node_id": node.id, "type": node.type, "model": model,
            "calls_min": calls_min, "calls_max": calls_max,
            "usd_min": usd_min, "usd_max": usd_max, "note": note,
        })
        calls_min_total += calls_min
        calls_max_total += calls_max
        usd_min_total += usd_min
        usd_max_total += usd_max

    return Estimate(
        total_usd_min=usd_min_total, total_usd_max=usd_max_total,
        calls_min=calls_min_total, calls_max=calls_max_total,
        per_node=tuple(per_node), unbounded_loops=tuple(unbounded_loops),
        unpriced_models=tuple(unpriced),
    )
