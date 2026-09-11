"""plan_compare.py — "why this plan", not just "which plan costs less".

CMP-08 (INFORME §3.7, `CONTRATO_CMP_W2.md` W2-B): given a goal and several
candidate workflow definitions for reaching it — typically `single_model`
(one model does everything), `agent_team` (several agent/skill nodes
collaborate) and `deterministic_steps` (as much as possible pushed into
non-model node types) — produce one comparison table instead of forcing
someone to open three `/api/workflows/estimate` responses and diff them by
eye.

This module adds no estimation logic of its own: every number in the table
comes straight from `src.workflow_cost_estimate.estimate_detailed`, called
once per plan with the SAME pricing/profile/latency inputs so no plan is
quietly estimated more generously than another. What this module contributes
is the table shape itself, and — the actual point of "por qué este plan" —
a `basis` tag on every cell:

* `"computed"` — a number the workflow's own structure fixes (no cycle, no
  unpriced model, no unknown skill profile touches it).
* `"estimated"` — a number that depends on at least one listed assumption
  (an unbounded cycle's `assumed_iterations`, or a token-per-call default).
* `"unknown"` — the plan touches something genuinely unpriced/uncharacterized
  (an unpriced model, a composite skill with no declared/historical calls
  profile) and the cell is reported as such rather than silently zeroed.

Pure and read-only, the same guarantee `estimate_detailed` makes: no network,
no LLM call. Nothing here decides which plan is "best" — `compare()` is a
table, not a verdict; ranking or picking a plan is left to whoever reads it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from src.workflow_cost_estimate import DetailedEstimate, ModelPrice, estimate_detailed

__all__ = ["PlanCell", "PlanRow", "PlanComparison", "compare"]

#: The three canonical shapes CMP-08 names, kept only as documentation of
#: what a caller's `plans[i]["id"]` conventionally is — this module accepts
#: any id/label, it never requires these three.
CANONICAL_PLAN_IDS = ("single_model", "agent_team", "deterministic_steps")


@dataclass(frozen=True)
class PlanCell:
    value: Any
    basis: str  # "computed" | "estimated" | "unknown"

    def to_dict(self) -> Dict[str, Any]:
        return {"value": self.value, "basis": self.basis}


@dataclass(frozen=True)
class PlanRow:
    metric: str
    cells: Dict[str, PlanCell]  # plan_id -> cell

    def to_dict(self) -> Dict[str, Any]:
        return {"metric": self.metric, "cells": {k: v.to_dict() for k, v in self.cells.items()}}


@dataclass(frozen=True)
class PlanComparison:
    goal: str
    plan_ids: Tuple[str, ...]
    plan_labels: Dict[str, str]
    rows: Tuple[PlanRow, ...]
    reasons: Dict[str, Tuple[str, ...]]       # plan_id -> cost_unestimable, "why this plan" evidence
    detail: Dict[str, Dict[str, Any]]         # plan_id -> full DetailedEstimate.to_dict()
    errors: Dict[str, str]                    # plan_id -> parse/estimate failure, if any (plan omitted from rows)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "goal": self.goal,
            "plan_ids": list(self.plan_ids),
            "plan_labels": dict(self.plan_labels),
            "rows": [r.to_dict() for r in self.rows],
            "reasons": {k: list(v) for k, v in self.reasons.items()},
            "detail": dict(self.detail),
            "errors": dict(self.errors),
        }


def _basis_for(cost_unestimable: Sequence[str], has_cycle: bool, *, touched_by_unknown: bool) -> str:
    if touched_by_unknown:
        return "unknown"
    if has_cycle:
        return "estimated"
    return "computed"


def compare(
    goal: str,
    plans: Sequence[Mapping[str, Any]],
    *,
    prices: Optional[Mapping[str, ModelPrice]] = None,
    price_source: str = "caller_prices",
    price_as_of: str = "",
    capability_pricing: Optional[Mapping[str, Mapping[str, Any]]] = None,
    skill_calls_profiles: Optional[Mapping[str, Any]] = None,
    skill_call_history: Optional[Mapping[str, Any]] = None,
    local_latency: Optional[Mapping[str, Any]] = None,
    assumed_iterations: int = 3,
    default_tokens_per_call: Tuple[int, int] = (1500, 500),
) -> PlanComparison:
    """`goal` is a free-text label carried through unchanged — this module
    does not interpret it, it only titles the table. `plans` is a sequence
    of `{"id": str, "label"?: str, "definition": WorkflowDefinition|dict}`;
    a plan whose `definition` fails `WorkflowDefinition.parse()` is kept out
    of `rows` and reported in `errors` instead of aborting the whole
    comparison — one bad plan should never hide the other two."""
    plan_ids: list = []
    plan_labels: Dict[str, str] = {}
    estimates: Dict[str, DetailedEstimate] = {}
    errors: Dict[str, str] = {}

    for i, plan in enumerate(plans):
        if not isinstance(plan, Mapping):
            continue
        plan_id = str(plan.get("id") or f"plan_{i}")
        plan_ids.append(plan_id)
        plan_labels[plan_id] = str(plan.get("label") or plan_id)
        definition = plan.get("definition")
        try:
            estimates[plan_id] = estimate_detailed(
                definition,
                prices=prices, price_source=price_source, price_as_of=price_as_of,
                capability_pricing=capability_pricing,
                skill_calls_profiles=skill_calls_profiles, skill_call_history=skill_call_history,
                default_tokens_per_call=default_tokens_per_call,
                assumed_iterations=assumed_iterations, local_latency=local_latency,
            )
        except Exception as exc:  # a bad plan is reported, never crashes the whole compare
            errors[plan_id] = str(exc)

    def _cell(plan_id: str, value: Any, *, touched_by_unknown: bool) -> PlanCell:
        est = estimates.get(plan_id)
        has_cycle = bool(est and est.forecast_with_assumptions.get("assumed_iterations") and
                          any("unbounded" in r for r in (est.assumptions or ())))
        return PlanCell(value=value, basis=_basis_for(est.cost_unestimable if est else (), has_cycle,
                                                        touched_by_unknown=touched_by_unknown))

    metrics = [
        ("node_activations_max", lambda e: e.node_activations["max"]),
        ("model_calls_max", lambda e: e.model_calls["max"]),
        ("external_ops_max", lambda e: e.external_ops["max"]),
        ("tokens_in_max", lambda e: e.tokens["in"]["max"]),
        ("tokens_out_max", lambda e: e.tokens["out"]["max"]),
        ("cost_known_usd_max", lambda e: e.cost_known_usd["max"]),
    ]
    rows: list = []
    for metric_name, getter in metrics:
        cells: Dict[str, PlanCell] = {}
        for plan_id in plan_ids:
            est = estimates.get(plan_id)
            if est is None:
                continue
            unknown = bool(est.cost_unestimable) and metric_name in (
                "model_calls_max", "tokens_in_max", "tokens_out_max", "cost_known_usd_max")
            cells[plan_id] = _cell(plan_id, getter(est), touched_by_unknown=unknown)
        rows.append(PlanRow(metric=metric_name, cells=cells))

    return PlanComparison(
        goal=str(goal or ""),
        plan_ids=tuple(plan_ids),
        plan_labels=plan_labels,
        rows=tuple(rows),
        reasons={pid: est.cost_unestimable for pid, est in estimates.items()},
        detail={pid: est.to_dict() for pid, est in estimates.items()},
        errors=errors,
    )
