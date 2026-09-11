# Topology: Mermaid, profile/workflow linting, cost estimate (Lote A4)

`gcjordi/aigraphstudio` ported onto REAL Faustus data — no drawing-only model
of its own. Three pure modules, wired onto three existing routers:

- `src/topology_export.py` — `future_to_mermaid`, `workflow_to_mermaid`.
- `src/agent_profile_lint.py` — `lint_profile`, `lint_all`, `lint_workflow`,
  `find_cycles`.
- `src/workflow_cost_estimate.py` — `estimate`, `ModelPrice`.

## Endpoints

| Method | Path | Body | Returns |
| --- | --- | --- | --- |
| `GET` | `/api/futures/{future_id}/mermaid` | — | `{"ok": true, "mermaid": "flowchart TD\n..."}` |
| `POST` | `/api/workflows/mermaid` | `{"definition": {...}}` (same shape as `/api/workflows/validate`) | `{"ok": true, "mermaid": "..."}` |
| `POST` | `/api/workflows/estimate` | `{"definition": {...}, "prices"?: {model_id: {"prompt_usd_per_1k", "completion_usd_per_1k"}}}` | `{"ok": true, "estimate": {...}}` |
| `GET` | `/api/agent-profiles/lint` | — | `{"ok": true, "findings": [...]}` — every finding for the whole catalogue |
| `GET` | `/api/agent-profiles/lint/{kind}/{profile_id}` | — | `{"ok": true, "findings": [...]}`, or `{"ok": false, "error": {...}}` if `kind`/`profile_id` is unknown |

All five require the same admin gate their existing routers already use
(`require_admin` — the futures/workflows/agent-profiles routes were already
admin-only before this lot; nothing new was added). The two `POST
/api/workflows/*` routes reuse `_definition_or_400`, so an invalid definition
is refused with the same `{path, reason, got}`-shaped message
`/api/workflows/validate` already gives — a diagram or an estimate is never
produced for something that could not run.

### Example — `/api/workflows/mermaid`

```
POST /api/workflows/mermaid
{"definition": {
  "id": "report.publish", "version": "1.0.0", "title": "Write and send",
  "nodes": [
    {"id": "start", "type": "manual"},
    {"id": "check", "type": "condition", "needs": ["start"],
     "config": {"when": {"left": {"path": "inputs.score"}, "op": "gte", "right": 50}}},
    {"id": "send", "type": "deliver", "needs": ["check"], "config": {"to": "ana@example.com"}}
  ]
}}
```

```json
{"ok": true, "mermaid": "flowchart TD\n    start[\"start<br/>manual\"]\n    check{\"check<br/>condition\"}\n    send[[\"send<br/>deliver\"]]\n    start --> check\n    check -->|\"inputs.score gte 50\"| send\n    ...classDef lines..."}
```

The edge leaving a `condition` node is labelled with a short summary of its
`config.when` — the one edge type in `WorkflowDefinition` that is actually
conditional (`condition_handler` marks the downstream branch `skipped`, not
run, when the check fails). Every other node type/edge is a plain dependency.

## Mermaid escaping

Both `future_to_mermaid` and `workflow_to_mermaid` escape `"`, `[`, `]`, `|`,
`<`, `>` and `&` in every label using Mermaid's own character-reference
syntax (`#quot;`, `#91;`, …), and fold node ids to `[A-Za-z0-9_]+`
(disambiguated on collision) — a title or strategy name with any of those
characters cannot break the diagram's own grammar.

## Linting: what is checked, and — importantly — what is not

`lint_profile`/`lint_all` walk `src/agent_profiles/catalog.py`'s five profile
types. These are **legal-but-suspicious** findings, distinct from the schema
errors `catalog.register`/`catalog._validate` already refuse at registration
time (a blocking check not in `checks`, an unknown `write_policy`, …) — those
never reach a running catalogue and are checked here again only as a defensive
re-validation for a *candidate* profile that has not been registered yet.

| Code | Severity | Fires when |
| --- | --- | --- |
| `LINT-BUDGET-NO-CAP` | warn | a `BudgetProfile` ceiling (`max_tokens`, `max_seconds`, `max_rounds`, `max_tool_calls`, `max_branches`, `max_retries`) is `0` — this codebase's own "0 = unlimited" convention (`src/autonomy_budget.py`) |
| `LINT-BUDGET-UNCAPPED-NETWORK-SPEND` | error | `allows_network=True` and `max_tokens == 0` — the "never pass to paid silently" case |
| `LINT-VERIF-NO-BLOCKING` | warn | a `VerificationProfile` has no `blocking` checks — nothing can stop a run |
| `LINT-VERIF-BLOCKING-UNDECLARED` | error | `blocking` names a check not in `checks` |
| `LINT-COLLAB-WRITE-NO-REVIEW` | info | `write_policy` is `scoped_write`/`write` and `review_own_work` is `False` |
| `LINT-COLLAB-UNKNOWN-WRITE-POLICY` / `LINT-COLLAB-UNKNOWN-SPEAK-POLICY` | error | value outside `catalog.WRITE_POLICIES`/`SPEAK_POLICIES` |
| `LINT-OUTPUT-REQUIRED-UNDECLARED` | error | `required` names a field not in `fields` |
| `LINT-CONTEXT-NO-BUDGET` / `LINT-CONTEXT-NO-ENGINE-REF` | error | `budget_tokens <= 0` / `engine_profile_id` blank |

`lint_workflow(definition)` walks one `WorkflowDefinition`'s `needs` graph:

| Code | Severity | Fires when |
| --- | --- | --- |
| `LINT-WF-CYCLE-NO-BOUND` | error | a strongly-connected component (Tarjan SCC) of size > 1, or a node whose `needs` names itself |
| `LINT-WF-UNREACHABLE` | error | no chain of `needs` from a root (`needs == ()`) reaches this node |
| `LINT-WF-OUTPUT-NO-EVALUATOR` | warn | a `deliver`/`artifact_store` node has no `condition`/`human_approval` node anywhere upstream |
| `LINT-WF-EFFECT-NO-HUMAN` | warn | a `skill`/`artifact_store`/`deliver` node is not `config.idempotent` and has no `human_approval` node upstream |

**What aigraphstudio's original design expects that this schema does not
have, and is therefore NOT implemented:** a bounded loop with a
`max_iterations`-style field. `WorkflowNode`/`WorkflowDefinition`
(`src/contracts/workflow.py`) have no such field, no `"loop"` node type, and
`WorkflowDefinition.parse()` already refuses any cycle outright — a cycle can
only exist on a definition assembled without going through `.parse()` (a
hand-built dataclass, an old stored row). Given that, `LINT-WF-CYCLE-NO-BOUND`
treats **every** cycle it finds as unbounded — there is no "bounded" case to
distinguish it from, so inventing the field to distinguish them would be
exactly what COMUN rule 6 (no invented fields) forbids. `find_cycles` is
exported standalone so `workflow_cost_estimate` can reuse the same detection
without duplicating Tarjan's algorithm.

`LINT-WF-UNREACHABLE` is, for the identical reason, unreachable on any
definition that went through `.parse()`: every node in a validated DAG is, by
induction, reachable from some root. It only ever fires on a hand-assembled
definition — a dangling `needs` reference (`.parse()` would have refused it:
"names [...], which no node ... defines") or a cycle with no root feeding
into it.

## Cost estimate: what is priced, and where the number comes from

`estimate(definition, *, prices=None, default_tokens_per_call=(1500, 500),
assumed_iterations=3)` returns an `Estimate`:

```json
{
  "total_usd_min": 0.0025, "total_usd_max": 0.0025,
  "calls_min": 2, "calls_max": 2,
  "per_node": [
    {"node_id": "start", "type": "manual", "model": "", "calls_min": 1, "calls_max": 1,
     "usd_min": 0.0, "usd_max": 0.0, "note": "not a model-invoking node type"},
    {"node_id": "work", "type": "skill", "model": "local:test-7b", "calls_min": 1, "calls_max": 1,
     "usd_min": 0.0025, "usd_max": 0.0025, "note": ""}
  ],
  "unbounded_loops": [],
  "unpriced_models": []
}
```
(with default `default_tokens_per_call=(1500, 500)` and `prices={"local:test-7b": {"prompt_usd_per_1k": 0.001, "completion_usd_per_1k": 0.002}}`.)

- **Only `skill` nodes are priced.** Every other node type
  (`manual`/`schedule`/`webhook`/`condition`/`wait`/`human_approval`/
  `artifact_store`/`deliver`) is structural or deterministic and cannot, by
  itself, call a model.
- **A `skill` node's model comes from `config.model`, read defensively.**
  Nothing in `WorkflowNode`/`WorkflowDefinition` requires or even mentions a
  `model` key — it is a convention this module reads with `.get(...)`, never
  enforced. A `skill` node with no `config.model` contributes `$0` and is
  named in `per_node[i].note`, not silently folded into the total.
- **Pricing itself is never invented.** `src/model_capabilities.py` and
  `src/model_capability_readers/openrouter.py` do not expose a `pricing`
  field today (checked before writing this module) — the OpenRouter reader's
  `ModelCapabilityRecord.raw` keeps the provider's raw payload, which MAY
  carry `pricing` once a live catalog is fetched, but nothing in this
  codebase indexes it by model id yet. So `prices: Mapping[str, ModelPrice]`
  is caller-supplied; a named model missing from it lands in
  `unpriced_models` with `$0` rather than a guess.
- **`calls_min`/`calls_max` bound the executions, not just the price.** A
  node downstream of a `condition` node (anywhere in its `needs` ancestry)
  might be skipped — `condition_handler`'s own docstring: a failed check
  marks the branch under it `skipped`, not run — so it contributes `0` to
  the cheapest case and `1` to the most expensive one. A node inside a cycle
  (see the linter section above — always unbounded in this schema)
  contributes `1` to the minimum and `assumed_iterations` (default `3`,
  overridable) to the maximum, and is named in `unbounded_loops`.

## What this lot did NOT do

- No pricing catalogue was added anywhere. If a future lot exposes real
  OpenRouter/local pricing through `src/model_capabilities.py`, `estimate`'s
  `prices` parameter is the integration point — the estimator itself does not
  need to change, only whichever caller wires a live `prices` mapping in.
- `lint_workflow` does not attempt to model `condition` branch semantics
  precisely (which nodes a specific condition outcome would actually skip);
  it only asks "is there ANY condition/human_approval upstream at all". The
  cost estimator's `condition`-gating is the same kind of approximation, in
  the direction of a min/max range rather than a single wrong number.
