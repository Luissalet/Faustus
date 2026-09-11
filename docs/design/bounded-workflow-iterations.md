# Bounded workflow iterations — a design proposal (CMP-07)

Status: **proposed, not implemented**. `src/contracts/workflow_iteration.py`
is the dataclass + validation half of this document, kept deliberately
disconnected from anything that runs: not imported by
`src/workflows/engine.py`, `src/workflows/handlers.py`, or any route.
`WorkflowDefinition.parse()` (`src/contracts/workflow.py`) still refuses
every cycle outright, `_find_cycle` untouched, and `NODE_TYPES` gains no
`"loop"` entry. This document is what a future lot would need to read
before wiring any of it in.

## Why this is a document first

`docs/api/topology.md` already says, twice, that this schema has no
bounded-loop concept: `agent_profile_lint.LINT-WF-CYCLE-NO-BOUND` treats
every cycle it finds as unbounded (there is no "bounded" case to
distinguish it from), and `workflow_cost_estimate.unbounded_loops` assumes
a fixed number of passes through any cycle it finds, by policy rather than
knowledge. Both of those are honest about a real gap. Filling that gap by
actually letting a workflow definition contain a cycle is a change to a
contract three other modules (the linter, the estimator, and now
`src/workflows/simulate.py`) already read structurally — worth settling on
paper, and testing as a paper design, before the engine's `advance()` has
to know about it.

## What a loop needs to be safe

A workflow is durable: a process can die between any two nodes and resume
from the store (`src/workflows/engine.py`'s own docstring). A loop inside
that same engine has to keep every one of the guarantees a normal node
already has, plus three more a single-pass node never needed:

1. **A hard ceiling that is never "unlimited".** Every OTHER budget
   dimension in this codebase uses `0 = unlimited`
   (`src/autonomy_budget.py`). A loop's iteration count cannot use that
   convention — an "unlimited" loop is exactly the unbounded cycle
   `WorkflowDefinition.parse()` already refuses, so a loop proposal that
   let `max_iterations: 0` mean "forever" would just reopen that refusal
   under a new name. `LoopBudget.max_iterations` is therefore always
   required and always positive (1–10,000).
2. **State per iteration, not per node.** A `NodeRun` today is one row per
   node per run. A loop body run five times needs five rows if a restart
   is going to resume from the SAME iteration it was on, not iteration 1.
   `IterationState` is that row: `(workflow_run_id, node_id, iteration) ->
   status`, reusing `workflow.NODE_STATUSES`/`TERMINAL_NODE` rather than a
   second status vocabulary.
3. **An idempotency key that is per (effect, iteration), not just per
   node.** `workflow.idempotency_key` collides two ATTEMPTS at the same
   node into the same key on purpose — that is what makes a retry safe.
   A loop body with an effectful node (`skill`/`artifact_store`/`deliver`)
   needs the OPPOSITE behaviour across iterations: iteration 3 and
   iteration 4 must NOT collide, because sending the email once per
   genuine new iteration is the loop's entire job. `loop_effect_
   idempotency_key` folds `iteration` into the fingerprint as its own
   named part for exactly this reason — see that function's docstring for
   why it is not merged into `config`/`inputs` instead.

## The proposed shape

```json
{
  "id": "review-loop", "type": "loop", "needs": ["draft"],
  "config": {
    "body": ["revise", "check"],
    "budget": {"max_iterations": 4, "max_tokens": 40000, "on_exhausted": "pause"},
    "until": {"left": {"path": "loop.results.check.passed"}, "op": "truthy"}
  }
}
```

- **`body`** names sibling node ids the same way `WorkflowNode.needs`
  already does — a subgraph *reference*, not a nested definition. This
  matters because every tool that walks a definition today
  (`agent_profile_lint`, `workflow_cost_estimate`, `src/topology_export
  .py`, `src/workflows/simulate.py`) reads `WorkflowDefinition.nodes` as a
  flat list; a loop whose body were a second, nested list of nodes would
  be invisible to all four without separately teaching each one to
  recurse. Keeping the body as a list of existing top-level node ids means
  a future engine change to run them repeatedly is additive, not a
  rewrite of every structural reader.
- **`budget`** is `LoopBudget` (above). `on_exhausted: "pause"` mirrors
  `WorkflowEngine._check_budget`'s real behaviour for a run-level budget —
  paused is a resumable state, not a failure; `"fail"` is offered for a
  loop author who genuinely wants a hard stop, but is never the default,
  matching the rest of this codebase's bias toward "stop and ask" over
  "stop and lose the work".
- **`until`** is optional and shaped exactly like `condition_handler`'s
  `when` (same closed operator set, kept in sync by hand and pinned by
  `tests/test_cmp07_workflow_iteration.py::
  test_loop_operators_stay_in_sync_with_the_real_condition_handler`,
  because `contracts/` must not import `workflows/handlers.py` — that
  dependency only runs the other way). It is a SEPARATE exit from
  `max_iterations`: reaching `until` stops the loop early; `max_iterations`
  is what makes accepting the loop safe even if `until` never fires (a
  `path` that nothing in the body ever sets is a real authoring mistake,
  not a hang — the ceiling still applies regardless).

## Cancellation

Not a new mechanism. `workflows/engine.py::_run_node` already threads
`context["cancel_requested"]` — a callback the running handler can poll —
through every node, backed by `WorkflowStore.claim_active`. A loop
iteration is proposed to poll the SAME callback between iterations (not
mid-iteration, since a loop body is itself an ordinary sub-DAG of ordinary
nodes that already get this for free): cancelling a run stops the loop
before its NEXT iteration starts, and `IterationState.status ==
"cancelled"` records exactly which iteration that was — reusing the
terminal vocabulary every other node already has, rather than inventing a
cancellation channel specific to loops.

## What this document does NOT settle (open for whoever wires this in)

- **How `body` nodes are scheduled relative to the rest of the graph.**
  Whether the loop node itself is a single entry in `ready_nodes()`'s
  runnable list that internally drives its body to completion each
  iteration, or whether each iteration re-exposes the body nodes to the
  normal scheduler with a per-iteration suffix on their id, is an engine
  design question this document deliberately leaves open — both are
  compatible with the `IterationState`/idempotency-key shape above, and
  picking one is exactly the "cableado al motor" this lot was told not to
  do.
- **Whether `LINT-WF-CYCLE-NO-BOUND` gains a "bounded" exemption.** Once a
  real `"loop"` node type exists, `agent_profile_lint.lint_workflow`
  would need to recognise a cycle wholly contained inside one `loop`
  node's own internal scheduling as bounded-by-design rather than an
  error — `find_cycles` over the FLAT `needs` graph (which a loop's body,
  referenced by id rather than nested, does not by itself turn into a
  cycle — see "The proposed shape" above) may not even need to change,
  depending on the scheduling design chosen above.
- **`workflow_cost_estimate`/`preflight` pricing a loop.** Both already
  have the concept of "an unbounded number of calls, reported rather than
  guessed" for a cycle; extending that to "a KNOWN ceiling of
  `max_iterations` calls, still reported rather than assumed to always
  hit the ceiling" is a small, compatible change once a loop is real, not
  attempted here.
- **UI.** `studio/src/screens/workflows/NodeInspector.tsx` (this same
  lot) has no loop-specific affordance; a loop node, if one is ever
  authored by hand and imported, would render through the same generic
  `config` JSON view every other node type falls back to today.
