"""
contracts/workflow_iteration.py — PROPOSED contract for a bounded workflow
loop (CMP-07, `docs/design/bounded-workflow-iterations.md`).

**This file is a design artifact, not a running feature.** It is dataclasses
and validation only: nothing here is imported by `src/workflows/engine.py`,
`src/workflows/handlers.py`, or any route. `WorkflowDefinition.parse()`
(`src/contracts/workflow.py`) still refuses every cycle outright — `_find_
cycle` is untouched by this file, unimported by it, and this module adds no
`"loop"` entry to `NODE_TYPES`. A `WorkflowNode` built today cannot express
a loop; this is what one WOULD look like if a future lot decides to wire it
in, following `docs/design/bounded-workflow-iterations.md`'s reasoning for
every field below.

Why propose the shape before the engine: `docs/api/topology.md` already
documents, twice (`agent_profile_lint`'s `LINT-WF-CYCLE-NO-BOUND` and
`workflow_cost_estimate`'s `unbounded_loops`), that this schema has no
bounded-loop concept and that every cycle found is therefore treated as
fully unbounded. Writing the shape down and validating it against real test
fixtures — WITHOUT touching the engine — lets a reviewer check the contract
holds together before anyone lets a workflow definition actually contain a
cycle.

Four things CMP-07 asked this contract to settle, each pinned to one piece
below:

* **`max_iterations`/`until`** — `LoopBudget.max_iterations` is the hard
  ceiling (always required, never 0-as-unlimited: an actually-unbounded
  loop is exactly the case `WorkflowDefinition.parse()` already refuses,
  and this proposal does not get to reopen that by another name).
  `LoopUntil` is a SEPARATE, optional early exit — reaching it stops the
  loop before `max_iterations`, but `max_iterations` is what makes the loop
  safe to accept even if `until` never fires (a `until` that references a
  path nothing ever sets is a real definition mistake, not a hang, because
  the iteration ceiling still applies).
* **estado por iteración** — `IterationState`, one row per (loop node,
  iteration), reusing `workflow.NODE_STATUSES`/`TERMINAL_NODE` from the
  sibling contract rather than inventing a second status vocabulary a
  reader would have to learn is or isn't the same one.
* **presupuesto** — `LoopBudget` also carries the same "0 = unlimited"
  convention `src/autonomy_budget.py` already uses for every OTHER
  dimension (tokens/seconds/tool calls) — deliberately NOT applied to
  `max_iterations` itself, per the point above. `LoopBudget.exhausted_by`
  is a pure decision function (given usage counters, which dimension — if
  any — is over) so a future engine integration has one already-tested
  place to call rather than re-deriving the comparison.
* **claves de idempotencia por efecto/iteración** —
  `loop_effect_idempotency_key` extends `workflow.idempotency_key` with the
  iteration number baked into the fingerprint: two attempts at iteration 3
  of the same node must collide (the whole point of an idempotency key),
  but iteration 3 and iteration 4 must NOT — a loop body with an effectful
  node (`skill`/`artifact_store`/`deliver`) has to be free to actually
  repeat that effect once per genuine new iteration while still refusing a
  retried attempt at the SAME iteration. Cancellation is deliberately not a
  new mechanism: `IterationState.status == "cancelled"` reuses the terminal
  vocabulary a real engine already has (`workflows/engine.py`'s
  `context["cancel_requested"]` callback checks the SAME kind of durable
  row this dataclass proposes) rather than inventing a second cancellation
  channel a loop would need and a normal node would not.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from .base import (
    ContractError, SCHEMA_VERSION, as_mapping, fingerprint, ident, one_of,
    reject_unknown, text, text_list, timestamp, whole,
)
from .workflow import NODE_STATUSES, TERMINAL_NODE

__all__ = [
    "LOOP_OPERATORS", "ON_BUDGET_EXHAUSTED", "LoopBudget", "LoopUntil",
    "LoopNodeConfig", "IterationState", "loop_effect_idempotency_key",
]

#: Kept identical, by hand, to `src/workflows/handlers.py::OPERATORS` —
#: `tests/test_cmp07_workflow_iteration.py::
#: test_loop_operators_stay_in_sync_with_the_real_condition_handler` fails
#: the moment the two drift, because `contracts/` must not import
#: `workflows/handlers.py` (the dependency runs the other way: `workflows`
#: already imports from `contracts`, and a contract importing the runtime
#: layer above it would be a cycle in the *module graph*, the one place a
#: cycle is never acceptable here).
LOOP_OPERATORS: Tuple[str, ...] = (
    "eq", "ne", "gt", "gte", "lt", "lte", "contains", "in", "exists", "truthy",
)

#: What a loop does when its budget runs out. `pause` mirrors
#: `WorkflowEngine._check_budget`'s real behaviour for a run-level budget —
#: paused is a first-class, resumable state, not a failure — so a loop
#: proposal that silently chose `fail` instead would be a worse default
#: than the one this codebase already ships. `fail` is offered because a
#: loop author may want a hard stop; it is never the default.
ON_BUDGET_EXHAUSTED: Tuple[str, ...] = ("pause", "fail")


@dataclass(frozen=True)
class LoopBudget:
    """A ceiling on one loop node. `max_iterations` is always a real,
    positive bound — see the module docstring for why this is not allowed
    to be "0 = unlimited" the way every other field here is."""

    max_iterations: int
    max_tokens: int = 0
    max_seconds: int = 0
    max_tool_calls: int = 0
    on_exhausted: str = "pause"

    _KEYS = ("max_iterations", "max_tokens", "max_seconds", "max_tool_calls", "on_exhausted")

    @classmethod
    def parse(cls, raw: Any, path: str = "budget") -> "LoopBudget":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        max_iterations = whole(data, "max_iterations", path, required=True, minimum=1, maximum=10_000)
        return cls(
            max_iterations=max_iterations,
            max_tokens=whole(data, "max_tokens", path, default=0, minimum=0) or 0,
            max_seconds=whole(data, "max_seconds", path, default=0, minimum=0) or 0,
            max_tool_calls=whole(data, "max_tool_calls", path, default=0, minimum=0) or 0,
            on_exhausted=one_of(data, "on_exhausted", path, choices=ON_BUDGET_EXHAUSTED,
                                required=False, default="pause"),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {"max_iterations": self.max_iterations, "max_tokens": self.max_tokens,
                "max_seconds": self.max_seconds, "max_tool_calls": self.max_tool_calls,
                "on_exhausted": self.on_exhausted}

    def exhausted_by(self, *, iterations_used: int, tokens_used: int = 0,
                     seconds_used: float = 0.0, tool_calls_used: int = 0) -> Optional[str]:
        """Which dimension (if any) this usage has exceeded — checked in the
        same fixed order every time so two callers never disagree about
        which limit "wins" when several are exceeded at once. A pure
        function: no clock read, no store, nothing but the numbers handed
        in — the caller (a future engine integration) owns measuring them."""
        if iterations_used >= self.max_iterations:
            return "max_iterations"
        if self.max_tokens and tokens_used >= self.max_tokens:
            return "max_tokens"
        if self.max_seconds and seconds_used >= self.max_seconds:
            return "max_seconds"
        if self.max_tool_calls and tool_calls_used >= self.max_tool_calls:
            return "max_tool_calls"
        return None


@dataclass(frozen=True)
class LoopUntil:
    """An early exit, shaped exactly like `condition_handler`'s `when` (same
    `left`/`op`/`right`, same closed operator set) so a reader who already
    knows one reads the other for free. Optional: a loop with no `until`
    always runs to `max_iterations` (or a `LoopUntil`-free early stop is not
    possible — the ceiling is the only exit)."""

    left: Any
    op: str
    right: Any = None

    _KEYS = ("left", "op", "right")

    @classmethod
    def parse(cls, raw: Any, path: str = "until") -> "LoopUntil":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        if "left" not in data:
            raise ContractError(f"{path}.left", "is required")
        op = one_of(data, "op", path, choices=LOOP_OPERATORS, required=False, default="truthy")
        if op not in ("exists", "truthy") and "right" not in data:
            raise ContractError(f"{path}.right", f"is required when op is {op!r}")
        return cls(left=data.get("left"), op=op, right=data.get("right"))

    def to_dict(self) -> Dict[str, Any]:
        return {"left": self.left, "op": self.op, "right": self.right}


@dataclass(frozen=True)
class LoopNodeConfig:
    """The proposed shape of `config` on a future `"loop"` `WorkflowNode`
    type. `body` names sibling node ids the same way `WorkflowNode.needs`
    already does — a subgraph reference, not a nested definition, so a loop
    body stays visible to every tool (the linter, the estimator, the
    simulator) that already walks `WorkflowDefinition.nodes` as a flat
    list. `known_node_ids`, when given, checks `body`/`until` actually name
    real siblings; omitted, this validates the shape alone (the same
    two-phase check `WorkflowDefinition.parse()` itself does: first every
    node in isolation, then cross-references against the full id set)."""

    body: Tuple[str, ...]
    budget: LoopBudget
    until: Optional[LoopUntil] = None
    idempotency_scope: str = "effect_and_iteration"

    _KEYS = ("body", "budget", "until")

    @classmethod
    def parse(cls, raw: Any, path: str = "loop_config", *,
              known_node_ids: Optional[Sequence[str]] = None) -> "LoopNodeConfig":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        body = text_list(data, "body", path, max_items=64)
        if not body:
            raise ContractError(f"{path}.body", "a loop needs at least one node id in its body")
        if len(set(body)) != len(body):
            raise ContractError(f"{path}.body", "names the same node id more than once")
        budget_raw = data.get("budget")
        if budget_raw is None:
            raise ContractError(f"{path}.budget", "is required")
        budget = LoopBudget.parse(budget_raw, f"{path}.budget")
        until_raw = data.get("until")
        until = LoopUntil.parse(until_raw, f"{path}.until") if until_raw is not None else None
        if known_node_ids is not None:
            known = set(known_node_ids)
            unknown = sorted(set(body) - known)
            if unknown:
                raise ContractError(f"{path}.body", f"names {unknown}, which no sibling node defines")
        return cls(body=body, budget=budget, until=until)

    def to_dict(self) -> Dict[str, Any]:
        return {"body": list(self.body), "budget": self.budget.to_dict(),
                "until": self.until.to_dict() if self.until else None,
                "idempotency_scope": self.idempotency_scope}


@dataclass(frozen=True)
class IterationState:
    """One row of durable truth about one iteration of one loop node —
    `workflows.engine.NodeRun`'s own reasoning ("a restart reads what is
    already recorded"), one level more granular. `status` reuses
    `workflow.NODE_STATUSES` (including `"cancelled"`) rather than a
    second vocabulary; `TERMINAL_NODE` from the same sibling module is the
    correct check for "this iteration is done" here too."""

    workflow_run_id: str
    node_id: str
    iteration: int
    status: str = "pending"
    idempotency_key: str = ""
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    reason: str = ""
    budget_used: Mapping[str, Any] = field(default_factory=dict)
    result: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    _KEYS = ("workflow_run_id", "node_id", "iteration", "status", "idempotency_key",
             "started_at", "ended_at", "reason", "budget_used", "result", "schema_version")

    @classmethod
    def parse(cls, raw: Any, path: str = "iteration_state") -> "IterationState":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        status = one_of(data, "status", path, choices=NODE_STATUSES, required=False, default="pending")
        iteration = whole(data, "iteration", path, required=True, minimum=1, maximum=10_000)
        ended = timestamp(data, "ended_at", path)
        if status in TERMINAL_NODE and not ended:
            raise ContractError(f"{path}.ended_at", f"is required once an iteration is '{status}'")
        budget_used = data.get("budget_used")
        if budget_used is not None and not isinstance(budget_used, Mapping):
            raise ContractError(f"{path}.budget_used", "expected an object", got=budget_used)
        result = data.get("result")
        if result is not None and not isinstance(result, Mapping):
            raise ContractError(f"{path}.result", "expected an object", got=result)
        return cls(
            workflow_run_id=text(data, "workflow_run_id", path, max_len=64),
            node_id=ident(data, "node_id", path),
            iteration=iteration,
            status=status,
            idempotency_key=text(data, "idempotency_key", path, required=False, max_len=64),
            started_at=timestamp(data, "started_at", path),
            ended_at=ended,
            reason=text(data, "reason", path, required=False, max_len=1000),
            budget_used=dict(budget_used or {}),
            result=dict(result or {}),
            schema_version=whole(data, "schema_version", path, default=SCHEMA_VERSION, minimum=1),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {"schema_version": self.schema_version, "workflow_run_id": self.workflow_run_id,
                "node_id": self.node_id, "iteration": self.iteration, "status": self.status,
                "idempotency_key": self.idempotency_key, "started_at": self.started_at,
                "ended_at": self.ended_at, "reason": self.reason,
                "budget_used": dict(self.budget_used), "result": dict(self.result)}


def loop_effect_idempotency_key(*, workflow_run_id: str, node_id: str, iteration: int,
                                config: Mapping[str, Any], inputs: Any = None) -> str:
    """`workflow.idempotency_key`, with the iteration folded into the
    fingerprint. Two attempts at the SAME iteration of the SAME node must
    collide (that is what makes a retry harmless); iteration N and N+1 must
    NOT, because the loop body's whole point is to repeat the effect once
    per genuine new iteration. `iteration` is therefore fingerprinted as its
    own named part rather than merged into `config`/`inputs` — folding it
    into `config` would make a caller's accidental omission of the iteration
    number silently collide two DIFFERENT iterations onto one key, which is
    the exact bug this key exists to make impossible."""
    if not isinstance(iteration, int) or isinstance(iteration, bool) or iteration < 1:
        raise ValueError(f"iteration must be a positive integer, got {iteration!r}")
    return fingerprint([
        ("run", workflow_run_id),
        ("node", node_id),
        ("iteration", iteration),
        ("config", dict(config or {})),
        ("inputs", inputs),
    ])
