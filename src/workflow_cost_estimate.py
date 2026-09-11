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

__all__ = [
    "ModelPrice", "Estimate", "estimate",
    "StructuredPrice", "PRICE_UNIT_PER_1M", "price_from_openrouter_raw",
    "CallsProfile", "calls_profile_from_mapping",
    "DetailedEstimate", "estimate_detailed",
    "local_latency_for", "local_latency_snapshot",
]

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


# ---------------------------------------------------------------------------
# CMP-08: a detailed estimate with separate accounts, structured prices and
# an honest structural/forecast/measured split.
#
# Added alongside `estimate()` above rather than folded into it — nothing
# that already calls `estimate()` (`preflight.py`'s `token_estimate`/`cost`,
# and `/api/workflows/estimate` with no `?detail=1`) has to adopt the richer
# shape to keep working; `routes/workflows_routes.py`'s `/estimate?detail=1`
# and `src/plan_compare.py` are the two new callers of `estimate_detailed`.
#
# Why this exists (INFORME §3.7, `CONTRATO_CMP_W2.md` W2-B): `estimate()`
# folds every node's activation count into one `calls_min/calls_max` and
# prices it as if a `skill` node always makes exactly one model call. Both
# are wrong for the questions CMP-08 asks:
#   * a `deliver`/`artifact_store` activation is an external operation, not
#     a model call — counting it as one would make "how many LLM calls will
#     this make" wrong on any workflow that writes an artifact;
#   * a composite skill (one that itself runs several model calls, e.g. a
#     multi-step research skill) is not "one call" just because it is one
#     `skill` node — pricing it as one silently undercounts tokens and cost.
# ---------------------------------------------------------------------------

#: `{amount_prompt_per_1m, amount_completion_per_1m, unit, currency, source,
#: as_of}` — CMP-08's structured price shape. Per-million, not per-thousand:
#: OpenRouter's own catalogue and most published tables price per million
#: tokens; keeping `ModelPrice` (per-1k, `estimate()`'s existing shape) as
#: the input and converting here — rather than asking every caller to
#: recompute — is the only place the factor-of-1000 exists.
PRICE_UNIT_PER_1M = "per_1M_tokens"


@dataclass(frozen=True)
class StructuredPrice:
    """A priced model with visible provenance — never a bare number, so the
    UI (and this module's own caller) can show where it came from instead of
    presenting every price as equally certain."""

    amount_prompt_per_1m: float
    amount_completion_per_1m: float
    currency: str
    source: str
    as_of: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "amount_prompt_per_1m": self.amount_prompt_per_1m,
            "amount_completion_per_1m": self.amount_completion_per_1m,
            "unit": PRICE_UNIT_PER_1M,
            "currency": self.currency,
            "source": self.source,
            "as_of": self.as_of,
        }


def price_from_openrouter_raw(raw: Mapping[str, Any], *, as_of: str = "") -> Optional[StructuredPrice]:
    """A `StructuredPrice` from one OpenRouter `/models` payload's own
    `pricing` object — `{"prompt": "0.0000008", "completion": "0.0000024"}`,
    USD per single token (OpenRouter's documented shape). This module never
    fetches that payload itself (no network in an estimate — see the module
    docstring's pricing section); the caller passes in whatever
    `src/model_capability_readers/openrouter.py` already put on
    `ModelCapabilityRecord.raw` from a catalogue fetch it made for its own
    reasons. Returns `None` — never a guess — when the shape does not match.
    """
    pricing = raw.get("pricing") if isinstance(raw, Mapping) else None
    if not isinstance(pricing, Mapping):
        return None
    try:
        prompt_per_token = float(pricing.get("prompt"))
        completion_per_token = float(pricing.get("completion"))
    except (TypeError, ValueError):
        return None
    return StructuredPrice(
        amount_prompt_per_1m=prompt_per_token * 1_000_000.0,
        amount_completion_per_1m=completion_per_token * 1_000_000.0,
        currency="USD", source="openrouter_pricing_field", as_of=as_of,
    )


def _structured_from_model_price(price: ModelPrice, *, source: str, as_of: str) -> StructuredPrice:
    return StructuredPrice(
        amount_prompt_per_1m=price.prompt_usd_per_1k * 1000.0,
        amount_completion_per_1m=price.completion_usd_per_1k * 1000.0,
        currency="USD", source=source, as_of=as_of,
    )


@dataclass(frozen=True)
class CallsProfile:
    """How much ONE activation of a composite skill really costs: model
    calls, external operations, and tokens per call — so two skills that
    both name the same underlying model do not collapse into the same
    estimate just because `workflow_cost_estimate` cannot see inside a
    `skill` node's own execution. `source` is `"declared"` (the skill's own
    manifest, read by the caller — this module never scans the filesystem)
    or `"history"` (`DATA_DIR/skill_call_history.json`, same caveat)."""

    model_calls: float
    external_ops: float
    tokens_in: float
    tokens_out: float
    source: str
    samples: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {"model_calls": self.model_calls, "external_ops": self.external_ops,
                "tokens_in": self.tokens_in, "tokens_out": self.tokens_out,
                "source": self.source, "samples": self.samples}


def calls_profile_from_mapping(raw: Any, *, source: str) -> Optional[CallsProfile]:
    """Build a `CallsProfile` from a plain mapping (a skill manifest's
    `calls_profile` field, or one entry of `skill_call_history.json`) — or
    `None` if the shape is not usable, never a guessed profile."""
    if not isinstance(raw, Mapping):
        return None
    try:
        return CallsProfile(
            model_calls=float(raw.get("model_calls", 0) or 0),
            external_ops=float(raw.get("external_ops", 0) or 0),
            tokens_in=float(raw.get("tokens_in", 0) or 0),
            tokens_out=float(raw.get("tokens_out", 0) or 0),
            source=source, samples=int(raw.get("samples", 0) or 0),
        )
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class DetailedEstimate:
    """CMP-08's separated accounts. Every count below is a `{min, max}`
    pair, computed the same "cheapest path / most expensive path" way
    `estimate()` already does (a `condition` ancestor gates 0..1, a cycle
    contributes 1..`assumed_iterations`) — but split by what actually kind
    of thing happens, instead of one blended `calls_min/calls_max`."""

    node_activations: Dict[str, int]
    model_calls: Dict[str, int]
    external_ops: Dict[str, int]
    tokens: Dict[str, Dict[str, int]]          # {"in": {min,max}, "out": {min,max}}
    cost_known_usd: Dict[str, float]           # {min, max} — only the priced portion
    cost_unestimable: Tuple[str, ...]          # every reason a total is incomplete
    structural_bounds: Dict[str, Any]          # graph shape alone, no assumed_iterations
    forecast_with_assumptions: Dict[str, Any]  # the numbers above, plus the assumptions used
    measured: Optional[Dict[str, Any]]         # a real run's numbers, or None (documented why)
    prices_used: Dict[str, Any]                # {model_id: StructuredPrice.to_dict()}
    per_node: Tuple[Dict[str, Any], ...]
    assumptions: Tuple[str, ...]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node_activations": dict(self.node_activations),
            "model_calls": dict(self.model_calls),
            "external_ops": dict(self.external_ops),
            "tokens": {k: dict(v) for k, v in self.tokens.items()},
            "cost_known_usd": dict(self.cost_known_usd),
            "cost_unestimable": list(self.cost_unestimable),
            "structural_bounds": dict(self.structural_bounds),
            "forecast_with_assumptions": dict(self.forecast_with_assumptions),
            "measured": dict(self.measured) if self.measured is not None else None,
            "prices_used": dict(self.prices_used),
            "per_node": [dict(row) for row in self.per_node],
            "assumptions": list(self.assumptions),
        }


_MEASURED_NOTE = (
    "src/workflows/store.py::usage_so_far records only an effectful node's "
    "activation count and wall-clock active_seconds (checked before writing "
    "this — see its own docstring); there is no per-node token or cost "
    "ledger for a workflow run today, so tokens_in/tokens_out/cost_usd stay "
    "null here rather than being backfilled from the forecast."
)


def estimate_detailed(
    definition: Any,
    *,
    prices: Optional[Mapping[str, ModelPrice]] = None,
    price_source: str = "caller_prices",
    price_as_of: str = "",
    capability_pricing: Optional[Mapping[str, Mapping[str, Any]]] = None,
    skill_calls_profiles: Optional[Mapping[str, Any]] = None,
    skill_call_history: Optional[Mapping[str, Any]] = None,
    default_tokens_per_call: Tuple[int, int] = (1500, 500),
    assumed_iterations: int = DEFAULT_ASSUMED_ITERATIONS,
    local_latency: Optional[Mapping[str, Any]] = None,
    run_measured: Optional[Mapping[str, Any]] = None,
) -> DetailedEstimate:
    """A CMP-08 estimate for one run of `definition`.

    Pure and read-only, same guarantee as `estimate()`: no network, no LLM
    call, nothing here reaches outside `definition` and the mappings passed
    in. In particular this function does NOT call
    `src/model_capability_readers/openrouter.py`, `src/gpu_policy.py`,
    `src/resource_admission.py`, `src/llm_core.py` or `src/vram_admission.py`
    itself — every one of those either hits a network endpoint
    (`gpu_policy.model_sizes`) or is ambient runtime state a pure estimate
    function has no business reaching into directly. The caller (the
    `/estimate` route) reads them and passes in only the numbers:
    `capability_pricing` (an already-fetched OpenRouter catalogue slice),
    `local_latency` (already computed from `resource_admission.status()` +
    `llm_core.local_speed()` + `vram_admission.reservations_snapshot()` +
    `gpu_policy.model_sizes()`), `skill_calls_profiles`/`skill_call_history`
    (already read from a skill's manifest / `DATA_DIR/skill_call_history.json`).
    An estimate with none of these passed in still returns a complete,
    honest answer — every optional account is `"unknown"`/`null` rather
    than silently 0.

    `local_latency`, when given, is `{model_id: {"load": ..., "queue": ...,
    "prefill_tps": ..., "generation_tps": ..., "memory_server": ...}}` — this
    function only threads it through onto `per_node` rows for `skill` nodes
    naming that model; it computes none of these numbers itself.
    """
    wf = _as_workflow_definition(definition)
    by_id = {n.id: n for n in wf.nodes}
    prompt_tokens, completion_tokens = default_tokens_per_call
    prices = prices or {}
    capability_pricing = capability_pricing or {}
    skill_calls_profiles = skill_calls_profiles or {}
    skill_call_history = skill_call_history or {}
    local_latency = local_latency or {}

    cycles = find_cycles(wf.nodes)
    cycle_members: Dict[str, int] = {}
    unbounded_loops: List[Dict[str, Any]] = []
    for cycle in cycles:
        unbounded_loops.append({"nodes": list(cycle), "assumed_iterations": assumed_iterations})
        for node_id in cycle:
            cycle_members[node_id] = assumed_iterations

    gated_ids = {
        node.id for node in wf.nodes
        if any(by_id[a].type == "condition" for a in _ancestors(by_id, node.id))
    }

    def _price_for(model: str) -> Tuple[Optional[StructuredPrice], Optional[str]]:
        """Returns `(price, reason_if_none)`. Prefers a caller-supplied
        `prices[model]` (explicit, per-call) unless `capability_pricing`
        names the same model with a real OpenRouter `pricing` field — the
        catalogue-sourced number wins only when it is actually present,
        never assumed."""
        raw = capability_pricing.get(model)
        from_catalogue = price_from_openrouter_raw(raw, as_of=price_as_of) if isinstance(raw, Mapping) else None
        if from_catalogue is not None:
            return from_catalogue, None
        model_price = prices.get(model)
        if model_price is not None:
            return _structured_from_model_price(model_price, source=price_source, as_of=price_as_of), None
        return None, f"model '{model}' has no price in `prices` or `capability_pricing`"

    def _calls_profile_for(skill_id: str) -> Tuple[Optional[CallsProfile], Optional[str]]:
        declared = calls_profile_from_mapping(skill_calls_profiles.get(skill_id), source="declared")
        if declared is not None:
            return declared, None
        history = calls_profile_from_mapping(skill_call_history.get(skill_id), source="history")
        if history is not None:
            return history, None
        return None, f"skill '{skill_id}' has no declared calls_profile or compatible history — model_calls unknown"

    per_node: List[Dict[str, Any]] = []
    reasons: List[str] = []
    seen_reasons: set = set()
    prices_used: Dict[str, Any] = {}

    node_act_min = node_act_max = 0
    model_calls_min = model_calls_max = 0
    ext_ops_min = ext_ops_max = 0
    tokens_in_min = tokens_in_max = 0
    tokens_out_min = tokens_out_max = 0
    usd_min_total = usd_max_total = 0.0

    def _note(reason: str) -> None:
        if reason not in seen_reasons:
            seen_reasons.add(reason)
            reasons.append(reason)

    for node in wf.nodes:
        if node.id in cycle_members:
            calls_min, calls_max = 1, max(1, cycle_members[node.id])
            structural_max: Any = "unbounded"
        elif node.id in gated_ids:
            calls_min, calls_max = 0, 1
            structural_max = 1
        else:
            calls_min, calls_max = 1, 1
            structural_max = 1
        node_act_min += calls_min
        node_act_max += calls_max

        row: Dict[str, Any] = {
            "node_id": node.id, "type": node.type,
            "structural_bounds": {"min": 0 if node.id in gated_ids else 1, "max": structural_max},
            "activations": {"min": calls_min, "max": calls_max},
            "is_model_call": False, "is_external_op": False,
            "model": "", "calls_profile_source": "n/a",
            "model_calls": {"min": 0, "max": 0},
            "external_ops": {"min": 0, "max": 0},
            "tokens_in": {"min": 0, "max": 0}, "tokens_out": {"min": 0, "max": 0},
            "usd": {"min": 0.0, "max": 0.0},
            "note": "not a model-invoking or external-operation node type",
        }

        if node.type == "skill":
            config = node.config if isinstance(node.config, Mapping) else {}
            model = str(config.get("model") or "").strip()
            skill_id = str(config.get("skill") or "").strip()
            row["model"] = model

            if skill_id:
                profile, reason = _calls_profile_for(skill_id)
            else:
                profile, reason = None, None  # a bare model call, not a named composite skill

            if profile is not None:
                mc_min = calls_min * profile.model_calls
                mc_max = calls_max * profile.model_calls
                eo_min = calls_min * profile.external_ops
                eo_max = calls_max * profile.external_ops
                tin_min = calls_min * profile.tokens_in
                tin_max = calls_max * profile.tokens_in
                tout_min = calls_min * profile.tokens_out
                tout_max = calls_max * profile.tokens_out
                row["calls_profile_source"] = profile.source
                row["note"] = f"skill '{skill_id}': calls_profile from {profile.source}"
            elif skill_id:
                # A composite skill we cannot characterize: never assumed to
                # be "one call" — it is unknown, and named as a reason the
                # totals below are incomplete.
                mc_min = mc_max = eo_min = eo_max = tin_min = tin_max = tout_min = tout_max = 0
                row["calls_profile_source"] = "unknown"
                row["note"] = reason or f"skill '{skill_id}': calls_profile unknown"
                _note(reason or row["note"])
            else:
                # A plain, non-composite skill node: one model call per
                # activation is the honest default — it is what `skill_handler`
                # actually does when there is no named sub-skill to expand.
                mc_min, mc_max = calls_min, calls_max
                tin_min, tin_max = calls_min * prompt_tokens, calls_max * prompt_tokens
                tout_min, tout_max = calls_min * completion_tokens, calls_max * completion_tokens
                eo_min = eo_max = 0
                row["calls_profile_source"] = "single_call_default"

            row["is_model_call"] = mc_max > 0
            row["model_calls"] = {"min": mc_min, "max": mc_max}
            row["external_ops"] = {"min": eo_min, "max": eo_max}
            row["tokens_in"] = {"min": tin_min, "max": tin_max}
            row["tokens_out"] = {"min": tout_min, "max": tout_max}
            model_calls_min += mc_min
            model_calls_max += mc_max
            ext_ops_min += eo_min
            ext_ops_max += eo_max
            tokens_in_min += tin_min
            tokens_in_max += tin_max
            tokens_out_min += tout_min
            tokens_out_max += tout_max

            if mc_max > 0:
                if not model:
                    row["note"] = (row["note"] + "; " if row["note"] else "") + "no config.model — cost unknown"
                    _note(f"node '{node.id}': no config.model — cost unknown, excluded from cost_known_usd")
                else:
                    price, price_reason = _price_for(model)
                    if price is None:
                        _note(price_reason or f"model '{model}' unpriced")
                    else:
                        prices_used[model] = price.to_dict()
                        per_call_min = ((tin_min / mc_min if mc_min else prompt_tokens) / 1_000_000.0) * price.amount_prompt_per_1m \
                            + ((tout_min / mc_min if mc_min else completion_tokens) / 1_000_000.0) * price.amount_completion_per_1m
                        usd_min = per_call_min * mc_min
                        usd_max = (tin_max / 1_000_000.0) * price.amount_prompt_per_1m \
                            + (tout_max / 1_000_000.0) * price.amount_completion_per_1m
                        row["usd"] = {"min": usd_min, "max": usd_max}
                        usd_min_total += usd_min
                        usd_max_total += usd_max
                        latency = local_latency.get(model)
                        if isinstance(latency, Mapping):
                            row["latency_estimate"] = dict(latency)

        elif node.type in ("artifact_store", "deliver"):
            row["is_external_op"] = True
            row["external_ops"] = {"min": calls_min, "max": calls_max}
            row["note"] = f"'{node.type}' node: an external operation, not a model call"
            ext_ops_min += calls_min
            ext_ops_max += calls_max

        per_node.append(row)

    if unbounded_loops:
        _note(f"{len(unbounded_loops)} unbounded cycle(s) — forecast assumes "
              f"{assumed_iterations} iterations each; structural_bounds leaves them unbounded")

    structural_bounds = {
        "node_activations": {"min": sum(0 if n.id in gated_ids else 1 for n in wf.nodes),
                              "max": "unbounded" if cycle_members else node_act_max},
        "note": "graph shape alone — no assumed_iterations applied to cycles, "
                "so a definition with an unbounded cycle reports max as the "
                "string \"unbounded\" here rather than a number.",
    }
    forecast_with_assumptions = {
        "node_activations": {"min": node_act_min, "max": node_act_max},
        "model_calls": {"min": model_calls_min, "max": model_calls_max},
        "external_ops": {"min": ext_ops_min, "max": ext_ops_max},
        "assumed_iterations": assumed_iterations,
        "default_tokens_per_call": {"prompt": prompt_tokens, "completion": completion_tokens},
    }

    assumptions: List[str] = []
    if unbounded_loops:
        assumptions.append(
            f"cycles (unbounded structurally) are forecast at {assumed_iterations} iterations each")
    if any(row["calls_profile_source"] == "single_call_default" for row in per_node):
        assumptions.append(
            f"a 'skill' node with a bare config.model (no calls_profile) is assumed to make "
            f"exactly 1 model call of {prompt_tokens} prompt + {completion_tokens} completion tokens")

    measured: Optional[Dict[str, Any]] = None
    if run_measured is not None:
        measured = {
            "tool_calls": run_measured.get("tool_calls"),
            "active_seconds": run_measured.get("active_seconds"),
            "tokens_in": None, "tokens_out": None, "cost_usd": None,
            "note": _MEASURED_NOTE,
        }

    return DetailedEstimate(
        node_activations={"min": node_act_min, "max": node_act_max},
        model_calls={"min": model_calls_min, "max": model_calls_max},
        external_ops={"min": ext_ops_min, "max": ext_ops_max},
        tokens={"in": {"min": tokens_in_min, "max": tokens_in_max},
                "out": {"min": tokens_out_min, "max": tokens_out_max}},
        cost_known_usd={"min": usd_min_total, "max": usd_max_total},
        cost_unestimable=tuple(reasons),
        structural_bounds=structural_bounds,
        forecast_with_assumptions=forecast_with_assumptions,
        measured=measured,
        prices_used=prices_used,
        per_node=tuple(per_node),
        assumptions=tuple(assumptions),
    )


# ---------------------------------------------------------------------------
# W3-C (CMP-08 follow-up): computing the `local_latency` mapping
# `estimate_detailed` above only threads through — never computes itself
# (see that function's own docstring: no network, no ambient runtime state
# from a pure function). `local_latency_for`/`local_latency_snapshot` are
# where that computation actually happens, kept OUT of `estimate_detailed`
# and `estimate` on purpose so both stay pure and testable with fakes alone.
# A caller that wants a real number (the `/estimate` route, some future
# orchestrator step) calls `local_latency_snapshot` first and passes its
# result in as `estimate_detailed(..., local_latency=...)`.
# ---------------------------------------------------------------------------

#: Every field a `local_latency` entry can carry — the same four this
#: module's own docstring and `estimate_detailed`'s promise, plus
#: `size_bytes` as extra context a UI can show even when `load` itself
#: stays unknown ("4.9 GB, load time unknown" reads better than nothing).
LOCAL_LATENCY_FIELDS = ("load", "queue", "prefill_tps", "generation_tps", "memory_server")


def _model_size_bytes(sizes: Mapping[str, int], model: str) -> int:
    """The same tag-normalisation `gpu_policy._size_for` uses, duplicated
    locally rather than imported: that name is private to `gpu_policy.py`
    (leading underscore), and reaching across a module boundary for a
    private helper is worse than four lines of the same lookup here."""
    name = str(model or "").strip()
    if not name:
        return 0
    if name in sizes:
        return int(sizes[name])
    short = name.split("/")[-1]
    for candidate in (short, f"{short}:latest", name.split(":")[0] + ":latest"):
        if candidate in sizes:
            return int(sizes[candidate])
    return 0


def local_latency_for(model: str, *, endpoint_url: str = "") -> Dict[str, Any]:
    """The CMP-08 `local_latency` entry for ONE model at ONE local endpoint.

    Every field starts `"unknown"` and is only ever replaced by a value this
    function can actually trace to a real signal — never a guess:

    * **`generation_tps`** — `src.llm_core.local_speed(model)`, the decode
      speed Faustus has itself learned from that model's own replies this
      process's lifetime (`llm_core.remember_local_speed`, fed by Ollama's
      `eval_count`/`eval_duration`). `"unknown"` until this model has
      actually replied once.
    * **`load`** — always `"unknown"`. `src.gpu_policy.model_sizes(endpoint_url)`
      gives a model's size on disk (checked before writing this — the size
      IS surfaced, as `size_bytes`, when the lookup succeeds), but nothing
      in this codebase measures how long loading that many bytes actually
      takes; `llm_core`'s only local timing table is decode speed, not load
      time (`llm_core._LOCAL_SPEED`, read via `local_speed` above). Per the
      W3-C contract: report `unknown` rather than estimate load time from
      size and a guessed disk/PCIe throughput.
    * **`queue`** — `src.resource_admission.status()`'s live counters for
      whichever pool `endpoint_url` belongs to
      (`resource_admission.pool_for_endpoint`): `{available,
      foreground_waiting, pool_id}` when the endpoint is in a defined pool,
      `"unknown"` when it is not (nothing is queuing there, by construction
      of that module — an endpoint outside any pool is not admission-gated
      at all) or `endpoint_url` was not given.
    * **`prefill_tps`**, **`memory_server`** — always `"unknown"`: neither
      `gpu_policy`, `llm_core` nor `resource_admission` (the three sources
      this function is scoped to) expose a prompt-processing rate or a
      per-server VRAM reservation figure; `src.vram_admission` has the
      latter but reading it is a separate lot's scope, not this one's.

    Never raises: every lookup is best-effort and a failure anywhere (a
    server unreachable, a malformed pool) degrades that one field to
    `"unknown"` rather than failing the whole estimate.
    """
    model = str(model or "").strip()
    row: Dict[str, Any] = {field_name: "unknown" for field_name in LOCAL_LATENCY_FIELDS}
    if not model:
        return row

    try:
        from src import llm_core
        tps = llm_core.local_speed(model)
        if tps is not None:
            row["generation_tps"] = tps
    except Exception:  # noqa: BLE001 — a latency hint must never break an estimate
        pass

    endpoint_url = str(endpoint_url or "").strip()
    if endpoint_url:
        try:
            from src import gpu_policy
            sizes = gpu_policy.model_sizes(endpoint_url)
            size_bytes = _model_size_bytes(sizes, model)
            if size_bytes:
                row["size_bytes"] = size_bytes
        except Exception:  # noqa: BLE001
            pass
        try:
            from src import resource_admission
            pool_id = resource_admission.pool_for_endpoint(endpoint_url)
            if pool_id:
                pools = (resource_admission.status() or {}).get("pools") or []
                pool_row = next((p for p in pools if p.get("pool_id") == pool_id), None)
                if pool_row is not None:
                    row["queue"] = {
                        "pool_id": pool_id,
                        "available": pool_row.get("available"),
                        "foreground_waiting": pool_row.get("foreground_waiting"),
                    }
        except Exception:  # noqa: BLE001
            pass

    return row


def local_latency_snapshot(endpoints_by_model: Mapping[str, str]) -> Dict[str, Dict[str, Any]]:
    """`{model_id: endpoint_url}` → the full `local_latency` mapping
    `estimate_detailed(..., local_latency=...)` expects — one
    `local_latency_for` call per model, batched so a caller (the `/estimate`
    route, once wired — see `docs/api/topology.md` §Estimate) can build it in
    one pass over a definition's `skill` nodes instead of calling this once
    per node by hand."""
    out: Dict[str, Dict[str, Any]] = {}
    for model, endpoint_url in (endpoints_by_model or {}).items():
        out[str(model)] = local_latency_for(model, endpoint_url=endpoint_url)
    return out
