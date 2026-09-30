# Bounded workflow iterations — a design proposal (CMP-07)

Status: **implemented**. `src/contracts/workflow_iteration.py` is the contract,
`src/workflows/loop.py` runs it, and `"loop"` is a real node type. The choices
this document left open are settled in "How it was wired" at the end. The text
below is kept as the reasoning the implementation follows; where it says "a
future lot", read "this one".

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

## How it was wired

- **Scheduling.** The loop is one runnable node. The engine still sees a flat,
  acyclic `needs` graph; `src/workflows/loop.py` runs the `body` (sibling node
  ids, in `needs` order) once per iteration inside that node's own claim and
  lease. Body nodes are never top-level runnable (`loop_body_ids`), a body node
  may depend only on its body siblings and on what the loop itself waits for,
  and nothing outside may depend on a body node, only on the loop.
- **Body types.** `agent, classify, extract, guard, condition, artifact_store,
  skill, deliver`. Not waits, approvals, triggers or another loop.
- **State.** One row per pass in `workflow_iteration_runs` (unique key =
  `loop_effect_idempotency_key`), updated as each body node settles, plus
  `workflow_loop_state` for what a person added to a ceiling. A restart
  re-enters the loop node (a loop is not itself effectful, so its expired lease
  is released), finds the open pass and resumes at the body node it was on.
  Finished passes are never run again.
- **Effects.** A body node that reaches outside is never repeated to find out
  whether it happened: a pass found open across such a node fails with
  `unknown_effect`, the same rule the engine applies to a top-level node.
- **Exit.** `until` is evaluated after every pass over a context with
  `loop.iteration`, `loop.results.<body node>` (this pass) and `loop.previous`
  (the pass before), plus the usual `inputs` and `results`. A loop without
  `until` runs `max_iterations` passes and completes. With `until`, running out
  of `max_iterations`, `max_seconds` (active time only) or `max_tool_calls`
  (effectful body nodes plus what agent turns report) is "exhausted":
  `on_exhausted: pause` parks the loop node for a person to extend
  (`POST /api/workflows/runs/{id}/nodes/{node}/extend`) or stop, `fail` ends it.
  `max_tokens` is refused: nothing meters tokens per pass, so it could never
  fire.
- **Run budget.** The run's own autonomy budget is consulted between passes,
  and pass spend is part of `WorkflowStore.usage_so_far`, so a loop cannot be
  the way out of it.
- **Cancellation.** A pass polls the node's `cancel_requested` between body
  nodes and hands it to each body handler, so the lease fence applies to body
  effects too.
- **Tools that read a definition.** `LINT-WF-CYCLE-NO-BOUND` is unchanged (a
  loop is not a cycle). New findings: `LINT-WF-LOOP-NO-UNTIL`,
  `LINT-WF-LOOP-EFFECT-UNMETERED`; a loop's `until` counts as the evaluator for
  its body. The cost estimate multiplies body nodes by the loop's iteration
  range and reports a number where a cycle could only be assumed. The
  simulation reports a loop as a unit (`loops`) and does not walk its body.
  Preflight counts model calls of loop bodies. The mermaid export draws a
  dashed "repeats up to N" edge from the loop to each body node.
- **Not done.** Nested loops, parallel iterations over a list, and token
  metering.
