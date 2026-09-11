"""
workflows/simulate.py — a structural, round-by-round walk of a workflow
definition, with nothing executed.

CMP-07 (INFORME §3.6, "diseñar, simular y depurar un plan"): before a
definition is ever authorized to run, answer three questions without
calling a single handler, touching the store, or reading any real run
input: which nodes WOULD activate and in what order, where a real run
would stop for a human, and which branches would NOT be taken. This is
deliberately not `WorkflowEngine.advance` run against a fake store —
`advance()` claims nodes, calls handlers, writes rows and threads a
heartbeat; `simulate()` shares nothing with it but the same `needs` graph
`ready_nodes`/`_stopping` already read, re-derived here independently and
read-only, so a test can spy on "nothing was ever called" the same way
`src/workflows/preflight.py`'s tests do.

**`needs` are AND dependencies, never control branches.** A node with
several `needs` only activates once EVERY one of them is satisfied. This
schema has no second edge type — no "true"/"false" edge, no "else" —  so a
node's only way past a `human_approval` or an undecided `condition` in its
ancestry is for that ancestor to actually resolve. A "visual path" in a
drawn graph that appears to go around one is not a real path this contract
expresses; `simulate()` never lets one through, and always says so in
`SimulationResult.warnings` so a reader does not mistake "this node is
technically reachable" for "this node can run without that gate" — exactly
the confusion INFORME §3.6 calls out by name.

**A `condition`'s outcome and a `human_approval`'s decision are guesses the
caller supplies, never computed.** `simulate()` never calls
`src/workflows/handlers.py::evaluate` — that reads a real run's live
inputs, and this module intentionally touches none (no `WorkflowStore`, no
`inputs` argument at all). `choices: Mapping[str, bool]` lets a caller say
"assume this node passes/is approved" (`True`) or "assume it fails/is
denied" (`False`) for one round of exploration; a `condition` or
`human_approval` node with no entry in `choices` is left UNDECIDED and
reported in `awaiting_choice`, together with every node downstream of it —
never silently assumed to pass, which would be exactly the kind of
optimistic guess a debugging tool must not make.

**Every `human_approval` node reached is a real pause**, whether or not the
caller supplied a `choices` entry for it — `human_waits` lists it either
way, because that is what a real run does regardless of what a caller is
choosing to explore past it in this one simulation call.

**Rounds are depth layers, not an arbitrary loop counter.** Each round only
resolves nodes whose dependencies were ALL settled by the END of the
previous round — a node discovered mid-round through work done earlier in
the SAME round has to wait for the next one. This keeps `rounds_max`
meaningful as a real cap on how many dependency layers deep the walk goes
(and gives `PlanGraph`'s "layers by depth" rendering a number to key off),
rather than collapsing an entire chain into round one by accident.

A cyclic `WorkflowDefinition` cannot reach this module in the first place —
`.parse()` already refuses one (`_find_cycle`, untouched by this file), and
`_as_workflow_definition` below parses a raw mapping the same way
`workflow_cost_estimate`/`preflight` do. So `simulate()` never needs its own
cycle handling; a hand-built definition that bypasses `.parse()` would
simply stall (its cyclic nodes never resolve and are reported in
`not_reached` once rounds run out), which is an honest answer, not a crash.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

from src.contracts.workflow import WorkflowDefinition

__all__ = ["SimulationResult", "simulate"]

#: The standing warning this module always attaches — see the module
#: docstring's "AND dependencies, never control branches" section.
_AND_NOT_BRANCH_WARNING = (
    "`needs` are AND dependencies: a node with several `needs` only activates "
    "once every one of them is satisfied. This schema has no separate "
    "true/false edge, so a drawn path that looks like it goes around a "
    "human_approval or an undecided condition is not a path this contract "
    "expresses — simulate() never lets a node through on that basis, and "
    "respects `needs` exactly as a real run would."
)

DEFAULT_ROUNDS_MAX = 25


def _as_workflow_definition(definition: Any) -> WorkflowDefinition:
    if isinstance(definition, WorkflowDefinition):
        return definition
    if isinstance(definition, Mapping):
        return WorkflowDefinition.parse(definition)
    raise TypeError("simulate expects a WorkflowDefinition or a mapping")


@dataclass(frozen=True)
class SimulationResult:
    """The outcome of one structural walk. See the module docstring for what
    each bucket means; a node id appears in exactly one of `activated`,
    `not_taken`, `awaiting_choice` or `not_reached` — never two, so a caller
    summing the four buckets always accounts for every node exactly once."""

    rounds: Tuple[Dict[str, Any], ...]
    activated: Tuple[str, ...]
    not_taken: Tuple[str, ...]
    human_waits: Tuple[str, ...]
    awaiting_choice: Dict[str, Dict[str, str]]
    not_reached: Tuple[str, ...]
    rounds_used: int
    rounds_max: int
    warnings: Tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rounds": [dict(r) for r in self.rounds],
            "activated": list(self.activated),
            "not_taken": list(self.not_taken),
            "human_waits": list(self.human_waits),
            "awaiting_choice": {k: dict(v) for k, v in self.awaiting_choice.items()},
            "not_reached": list(self.not_reached),
            "rounds_used": self.rounds_used,
            "rounds_max": self.rounds_max,
            "warnings": list(self.warnings),
        }


def simulate(
    definition: Any,
    *,
    choices: Optional[Mapping[str, bool]] = None,
    rounds_max: int = DEFAULT_ROUNDS_MAX,
) -> SimulationResult:
    """A structural walk of `definition`, `rounds_max` layers deep at most.

    Pure and read-only: no handler is imported, no store is opened, no
    network of any kind. `definition` may already be a `WorkflowDefinition`
    or a raw dict in the shape `POST /api/workflows/validate` accepts —
    parsing it the same way means a cyclic or otherwise invalid definition
    is refused with the same `ContractError` that route would raise, rather
    than being "simulated" as if it could run.

    `choices` only ever narrows a `condition`/`human_approval` node's own
    outcome for THIS call; every other node type has no outcome to choose —
    it activates once its dependencies do, or it does not.
    """
    if isinstance(rounds_max, bool) or not isinstance(rounds_max, int) or rounds_max < 1:
        raise ValueError("rounds_max must be a positive integer")
    wf = _as_workflow_definition(definition)
    choices = dict(choices or {})

    by_id = {n.id: n for n in wf.nodes}
    outcome: Dict[str, str] = {}          # node_id -> "activated" | "not_taken"
    undecided: Dict[str, Dict[str, str]] = {}  # node_id -> {kind, blocked_by}
    human_waits: List[str] = []
    remaining = set(by_id)
    rounds: List[Dict[str, Any]] = []
    round_no = 0

    while remaining and round_no < rounds_max:
        round_no += 1
        resolved_this_round: Dict[str, str] = {}
        undecided_this_round: Dict[str, Dict[str, str]] = {}
        human_this_round: List[str] = []
        made_progress = False

        for node_id in remaining:
            node = by_id[node_id]

            # A need that already lost its branch cascades: this node can
            # never run either, regardless of anything else about it.
            if any(outcome.get(dep) == "not_taken" for dep in node.needs):
                resolved_this_round[node_id] = "not_taken"
                made_progress = True
                continue

            # A need still waiting on a choice blocks this node the same
            # way — propagate the SAME reason, so a long chain downstream
            # of one undecided gate all point back to the one thing that
            # would unblock them.
            blocking_dep = next((dep for dep in node.needs if dep in undecided), None)
            if blocking_dep is not None:
                undecided_this_round[node_id] = dict(undecided[blocking_dep])
                made_progress = True
                continue

            # Not every need has resolved yet — try again once a later
            # round (working from a fresh snapshot) has settled them.
            if not all(outcome.get(dep) == "activated" for dep in node.needs):
                continue

            if node.type == "human_approval":
                human_this_round.append(node_id)

            if node.type in ("condition", "human_approval"):
                choice = choices.get(node_id)
                if choice is None:
                    undecided_this_round[node_id] = {
                        "kind": node.type, "blocked_by": node_id,
                        "reason": f"no `choices[{node_id!r}]` was given, so whether this "
                                  f"{node.type} passes cannot be assumed",
                    }
                    made_progress = True
                    continue
                resolved_this_round[node_id] = "activated" if choice else "not_taken"
                made_progress = True
                continue

            # manual/schedule/webhook/skill/wait/artifact_store/deliver: a
            # structural pass-through once every dependency is satisfied.
            resolved_this_round[node_id] = "activated"
            made_progress = True

        if not made_progress:
            break

        outcome.update(resolved_this_round)
        undecided.update(undecided_this_round)
        for node_id in human_this_round:
            if node_id not in human_waits:
                human_waits.append(node_id)
        settled_ids = set(resolved_this_round) | set(undecided_this_round)
        remaining -= settled_ids

        rounds.append({
            "round": round_no,
            "activated": sorted(nid for nid, o in resolved_this_round.items() if o == "activated"),
            "not_taken": sorted(nid for nid, o in resolved_this_round.items() if o == "not_taken"),
            "human_waits": sorted(human_this_round),
            "newly_awaiting_choice": sorted(undecided_this_round),
        })

    warnings = [_AND_NOT_BRANCH_WARNING]
    if remaining:
        warnings.append(
            f"{len(remaining)} node(s) were never reached within rounds_max={rounds_max}: "
            f"{sorted(remaining)}. Raise rounds_max to see further, or this may be a "
            "definition that bypassed WorkflowDefinition.parse() and contains a cycle "
            "(.parse() itself already refuses one)."
        )

    return SimulationResult(
        rounds=tuple(rounds),
        activated=tuple(sorted(nid for nid, o in outcome.items() if o == "activated")),
        not_taken=tuple(sorted(nid for nid, o in outcome.items() if o == "not_taken")),
        human_waits=tuple(human_waits),
        awaiting_choice=dict(undecided),
        not_reached=tuple(sorted(remaining)),
        rounds_used=round_no,
        rounds_max=rounds_max,
        warnings=tuple(warnings),
    )
