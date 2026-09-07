"""Strict contracts for futures, branches, results and selections."""
from __future__ import annotations

import math
from typing import Any, Dict, Mapping, Tuple

from src.contracts.base import ContractError, as_mapping, flag, reject_unknown, text, text_list

MODES: Tuple[str, ...] = ("plan_only", "simulate", "prototype", "isolated_execute", "shadow", "canary")
FUTURE_STATUSES: Tuple[str, ...] = (
    "draft", "running", "evaluating", "ready_for_selection", "selected", "committing",
    "committed", "cancelled", "failed", "inconclusive",
)
BRANCH_STATUSES: Tuple[str, ...] = ("pending", "running", "completed", "failed", "pruned", "cancelled", "inconclusive")


class BranchingError(ContractError):
    pass


def strategy_request(value: Any, *, path: str = "strategy", default_id: str = "strategy") -> Dict[str, Any]:
    """Normalize one strategy, including fusion-created strategies.

    Futures and later fusion branches must cross the same contract boundary;
    accepting an arbitrary mapping during fusion made the supposedly strict
    persisted shape depend on which endpoint created the branch.
    """
    if not isinstance(value, Mapping):
        raise BranchingError(path, "must be an object", got=value)
    unknown = set(value) - {
        "id", "title", "description", "agent_profile_id", "context_supplements", "metadata",
    }
    if unknown:
        raise BranchingError(path, f"unknown keys: {sorted(unknown)}")
    ident = str(value.get("id") or default_id).strip()
    if not ident:
        raise BranchingError(f"{path}.id", "must be non-blank")
    supplements = value.get("context_supplements", [])
    if not isinstance(supplements, list) or any(not isinstance(x, str) for x in supplements):
        raise BranchingError(f"{path}.context_supplements", "must be a list of strings")
    metadata = value.get("metadata", {})
    if not isinstance(metadata, Mapping):
        raise BranchingError(f"{path}.metadata", "must be an object")
    return {
        "id": ident,
        "title": str(value.get("title") or ident).strip()[:240],
        "description": str(value.get("description") or "").strip()[:8000],
        "agent_profile_id": str(value.get("agent_profile_id") or "").strip()[:200],
        "context_supplements": [x.strip() for x in supplements if x.strip()],
        "metadata": dict(metadata),
    }


def future_request(value: Any) -> Dict[str, Any]:
    data = as_mapping(value, "future")
    allowed = {"title", "intent", "project_id", "session_id", "mode", "strategies",
               "criteria", "base_snapshot", "budget", "auto_select"}
    reject_unknown(data, allowed, "future")
    mode = text(data, "mode", "future", required=False, default="simulate", max_len=40)
    if mode not in MODES:
        raise BranchingError("future.mode", f"must be one of {list(MODES)}", got=mode)
    strategies = data.get("strategies", [])
    if not isinstance(strategies, list) or not 2 <= len(strategies) <= 12:
        raise BranchingError("future.strategies", "must contain 2..12 strategies", got=strategies)
    normalized = []
    seen = set()
    for index, raw in enumerate(strategies):
        strategy = strategy_request(
            raw,
            path=f"future.strategies[{index}]",
            default_id=f"strategy_{index + 1}",
        )
        ident = strategy["id"]
        if not ident or ident in seen:
            raise BranchingError(f"future.strategies[{index}].id", "must be non-blank and unique", got=ident)
        seen.add(ident)
        normalized.append(strategy)
    criteria = data.get("criteria", [])
    if not isinstance(criteria, list) or not criteria:
        criteria = [{"name": "quality", "weight": 1.0, "direction": "max"}]
    cleaned_criteria = []
    criterion_names = set()
    for index, raw in enumerate(criteria):
        if not isinstance(raw, Mapping):
            raise BranchingError(f"future.criteria[{index}]", "must be an object")
        name = str(raw.get("name") or "").strip()
        direction = str(raw.get("direction") or "max").strip()
        weight = raw.get("weight", 1.0)
        kind = str(raw.get("kind") or "scored").strip()
        unknown = set(raw) - {"name", "weight", "direction", "kind"}
        if unknown:
            raise BranchingError(f"future.criteria[{index}]", f"unknown keys: {sorted(unknown)}")
        if (not name or name in criterion_names or direction not in ("max", "min")
                or kind not in ("blocking", "scored", "metric")
                or isinstance(weight, bool) or not isinstance(weight, (int, float))
                or not math.isfinite(float(weight)) or weight < 0):
            raise BranchingError(f"future.criteria[{index}]", "requires name, direction max|min and non-negative weight")
        criterion_names.add(name)
        cleaned_criteria.append({"name": name, "weight": 0.0 if kind == "blocking" else float(weight),
                                 "direction": direction, "kind": kind})
    base = data.get("base_snapshot", {})
    budget = data.get("budget", {})
    if not isinstance(base, Mapping) or not isinstance(budget, Mapping):
        raise BranchingError("future", "base_snapshot and budget must be objects")
    allowed_budget = {"max_branches", "max_total_cost", "max_total_tokens", "max_wall_seconds"}
    unknown_budget = set(budget) - allowed_budget
    if unknown_budget:
        raise BranchingError("future.budget", f"unknown keys: {sorted(unknown_budget)}")
    defaults = {"max_branches": len(normalized), "max_total_cost": 0.0,
                "max_total_tokens": 0, "max_wall_seconds": 0.0}
    normalized_budget = dict(defaults)
    for key, raw in budget.items():
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(float(raw)) or raw < 0:
            raise BranchingError(f"future.budget.{key}", "must be a finite non-negative number", got=raw)
        normalized_budget[key] = int(raw) if key in ("max_branches", "max_total_tokens") else float(raw)
    if not 2 <= int(normalized_budget["max_branches"]) <= 12:
        raise BranchingError("future.budget.max_branches", "must be 2..12")
    if len(normalized) > int(normalized_budget["max_branches"]):
        raise BranchingError("future.strategies", "contains more branches than the declared budget")
    # Different ids are labels, not different strategies.  Refuse duplicate
    # bodies so branching buys genuine diversity rather than extra cost.
    bodies = set()
    for index, strategy in enumerate(normalized):
        body = (strategy["title"].casefold(), strategy["description"].casefold(),
                strategy["agent_profile_id"], tuple(strategy["context_supplements"]))
        if body in bodies:
            raise BranchingError(f"future.strategies[{index}]", "duplicates another strategy")
        bodies.add(body)
    return {"title": text(data, "title", "future", max_len=240),
            "intent": text(data, "intent", "future", max_len=8000),
            "project_id": text(data, "project_id", "future", required=False, default="", max_len=200),
            "session_id": text(data, "session_id", "future", required=False, default="", max_len=200),
            "mode": mode, "strategies": normalized, "criteria": cleaned_criteria,
            "base_snapshot": dict(base), "budget": normalized_budget,
            "auto_select": flag(data, "auto_select", "future", default=False)}


def result_request(value: Any) -> Dict[str, Any]:
    data = as_mapping(value, "result")
    allowed = {"status", "summary", "artifact_refs", "changeset_refs", "delta_refs",
               "proof_refs", "scores", "cost", "limitations", "state_projection_id",
               "context_packet_id", "environment_fingerprint", "gate_results"}
    reject_unknown(data, allowed, "result")
    status = text(data, "status", "result", required=False, default="completed", max_len=40)
    if status not in BRANCH_STATUSES or status in ("pending", "running"):
        raise BranchingError("result.status", "must be a terminal branch status", got=status)
    scores, cost = data.get("scores", {}), data.get("cost", {})
    if not isinstance(scores, Mapping) or not isinstance(cost, Mapping):
        raise BranchingError("result", "scores and cost must be objects")
    for key, score in scores.items():
        if (isinstance(score, bool) or not isinstance(score, (int, float))
                or not math.isfinite(float(score))):
            raise BranchingError(f"result.scores.{key}", "must be a finite number", got=score)
    gate_results = data.get("gate_results", {})
    if not isinstance(gate_results, Mapping):
        raise BranchingError("result.gate_results", "must be an object")
    cleaned_gates = {}
    for name, raw in gate_results.items():
        if not isinstance(raw, Mapping):
            raise BranchingError(f"result.gate_results.{name}", "must be an object")
        status_value = str(raw.get("status") or "").strip()
        if status_value not in ("passed", "failed", "inconclusive", "not_run"):
            raise BranchingError(f"result.gate_results.{name}.status", "must be passed, failed, inconclusive or not_run")
        refs = raw.get("proof_refs", [])
        if not isinstance(refs, list) or any(not isinstance(x, str) or not x.strip() for x in refs):
            raise BranchingError(f"result.gate_results.{name}.proof_refs", "must be a list of references")
        cleaned_gates[str(name)] = {"status": status_value, "proof_refs": list(dict.fromkeys(refs)),
                                    "reason": str(raw.get("reason") or "")[:2000]}
    for key, value in cost.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or value < 0:
            raise BranchingError(f"result.cost.{key}", "must be a finite non-negative number", got=value)
    return {"status": status,
            "summary": text(data, "summary", "result", required=False, default="", max_len=12000),
            "artifact_refs": list(text_list(data, "artifact_refs", "result", max_items=500, max_len=1000)),
            "changeset_refs": list(text_list(data, "changeset_refs", "result", max_items=500, max_len=1000)),
            "delta_refs": list(text_list(data, "delta_refs", "result", max_items=500, max_len=1000)),
            "proof_refs": list(text_list(data, "proof_refs", "result", max_items=500, max_len=1000)),
            "scores": {str(k): float(v) for k, v in scores.items()},
            "gate_results": cleaned_gates,
            "cost": {str(k): float(v) for k, v in cost.items()},
            "limitations": list(text_list(data, "limitations", "result", max_items=100, max_len=2000)),
            "state_projection_id": text(data, "state_projection_id", "result", required=False, default="", max_len=1000),
            "context_packet_id": text(data, "context_packet_id", "result", required=False, default="", max_len=1000),
            "environment_fingerprint": text(data, "environment_fingerprint", "result", required=False, default="", max_len=1000)}
