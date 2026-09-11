# Topology: Mermaid, profile/workflow linting, cost estimate (Lote A4 + ADP-16/17)

`gcjordi/aigraphstudio` ported onto REAL Faustus data — no drawing-only model
of its own. Pure modules, wired onto existing routers:

- `src/topology_export.py` — `future_to_mermaid`, `workflow_to_mermaid`.
- `src/agent_profile_lint.py` — `lint_profile`, `lint_all`, `lint_workflow`,
  `find_cycles`.
- `src/workflow_cost_estimate.py` — `estimate`, `ModelPrice`.
- `src/workflows/preflight.py` (ADP-16, W1-G) — `preflight`: connections,
  tools, permissions, inputs, outputs, human waits, a token estimate and a
  cost estimate for a definition, with zero LLM/script/network effects. See
  "Preflight" below.
- `src/workflows/interchange.py` (ADP-17, W1-G) — `export_canonical`,
  `import_external`: a versioned round-trip envelope, and an importer that
  maps only a checked subset of an external graph's node types into
  something executable. See "Interchange" below.

## Endpoints

| Method | Path | Body | Returns |
| --- | --- | --- | --- |
| `GET` | `/api/futures/{future_id}/mermaid` | — | `{"ok": true, "mermaid": "flowchart TD\n..."}` |
| `POST` | `/api/workflows/mermaid` | `{"definition": {...}}` (same shape as `/api/workflows/validate`) | `{"ok": true, "mermaid": "..."}` |
| `POST` | `/api/workflows/estimate` | `{"definition": {...}, "prices"?: {model_id: {"prompt_usd_per_1k", "completion_usd_per_1k"}}}` | `{"ok": true, "estimate": {...}}` |
| `POST` | `/api/workflows/preflight` | `{"definition": {...}, "prices"?: {...}, "installed_models"?: [...]}` | `{"ok": true, "preflight": {...}}` |
| `POST` | `/api/workflows/export` | `{"definition": {...}, "layout"?, "design_only"?, "provenance"?}` | `{"ok": true, "export": {...}}` |
| `GET` | `/api/workflows/runs/{run_id}/export` | — | `{"ok": true, "run_id": ..., "export": {...}}` |
| `POST` | `/api/workflows/import` | canonical envelope or aigraphstudio-shaped payload | `{"ok": true, "definition", "design_only", "rejected", "executable"}` |
| `GET` | `/api/agent-profiles/lint` | — | `{"ok": true, "findings": [...]}` — every finding for the whole catalogue |
| `GET` | `/api/agent-profiles/lint/{kind}/{profile_id}` | — | `{"ok": true, "findings": [...]}`, or `{"ok": false, "error": {...}}` if `kind`/`profile_id` is unknown |

All of these require the same admin gate their existing routers already use
(`require_admin` — the futures/workflows/agent-profiles routes were already
admin-only before Lote A4; nothing new was added). Every `POST
/api/workflows/*` route that takes a `definition` reuses `_definition_or_400`,
so an invalid definition is refused with the same `{path, reason, got}`-shaped
message `/api/workflows/validate` already gives — a diagram, an estimate or a
preflight report is never produced for something that could not run. `/import`
is the one exception by design: an unrecognized format or an unsupported
graph is a normal outcome there (`executable: false`), not a 4xx — see
"Interchange" below.

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

## Preflight: a dry-run before authorizing a plan (ADP-16)

`src/workflows/preflight.py::preflight(definition, *, installed_models=None,
prices=None, default_tokens_per_call=(1500, 500), assumed_iterations=3)`
returns a `Preflight` — pure and read-only, same guarantee `estimate` and
`lint_workflow` already make: no LLM call, no script, no download.

| Method | Path | Body | Returns |
| --- | --- | --- | --- |
| `POST` | `/api/workflows/preflight` | `{"definition": {...}, "prices"?: {...}, "installed_models"?: [...]}` (same `prices` shape as `/estimate`) | `{"ok": true, "preflight": {...}}` |

```json
{
  "connections": ["deliver:email", "skill:summarize"],
  "tools": ["summarize"],
  "permissions_required": ["net.read"],
  "inputs": ["inputs.score"],
  "outputs": [{"node_id": "send", "type": "deliver", "title": "send"}],
  "human_waits": ["approve"],
  "token_estimate": {"scenario_tokens": 2000, "conservative_tokens": 2000,
                      "per_node": [...], "basis": "assumes 1500 prompt + 500 completion tokens per call — a fixed assumption, not a measurement"},
  "cost": {"scenario": "unknown", "conservative": "unknown",
           "unpriced": ["local:test-7b"], "per_node": [...],
           "basis": "cost.per_node is a per-call USD price ...; cost.scenario/conservative is a whole-run budget guess ..."},
  "warnings": [{"code": "LINT-WF-EFFECT-NO-HUMAN", ...}]
}
```

- **`connections`/`tools`** are read the same defensive way `workflow_cost_estimate`
  reads `config.model`: a `skill` node's `config.skill` (a `media:` prefix
  becomes the single connection `"media"`), a `deliver` node's `config.backend`.
  Neither field is enforced by `WorkflowNode` — a node that names neither
  contributes nothing here rather than a guessed placeholder.
- **`inputs`** is `inputs.<key>` paths named in a `condition` node's
  `config.when` — the one place this schema expresses "read from the run's
  inputs" as a structured path. A workflow that only reaches an input
  through a `skill`/`deliver` node's opaque `config` is not listed; this gap
  is real, not hidden (see the module docstring).
- **`cost.scenario`/`cost.conservative` are `"unknown"`, not a partial
  number, whenever ANY invoked `skill` node cannot be priced** — either no
  `config.model` at all, or a named model missing from `prices`. Both cases
  land in `cost.unpriced` (a model id, or `"node:<id> (no config.model)"`),
  and `cost.per_node` keeps the per-call breakdown for whatever IS priced.
  This is stricter than `workflow_cost_estimate.estimate()` on its own,
  which silently treats "no `config.model`" as a `$0` contribution with only
  a `note` — `preflight` refuses to let that read as a complete total.
- **`cost.per_node` (per-token price) and `cost.scenario`/`cost.conservative`
  (a whole-run budget guess) are different fields on purpose** — ADP-16's
  own acceptance criterion. A caller that reads a per-token number as a
  spending cap is reading the wrong field.
- **`token_estimate` is always numeric** (never `"unknown"`) — it is a fixed
  assumption (`default_tokens_per_call`) applied to `calls_min`/`calls_max`,
  the same numbers `workflow_cost_estimate` computes; its `basis` string says
  so explicitly, because a token count nobody measured is a different kind
  of uncertain than a price nobody set.
- **`installed_models`** only annotates `token_estimate.per_node[i]
  .model_installed_locally` (`true`/`false`/`null` for "not checked") — it
  never changes `cost`, `connections`, or anything else.
- **`warnings`** is `agent_profile_lint.lint_workflow(definition)` verbatim —
  the same findings `/api/agent-profiles/lint` shape, not a second rulebook.

## Interchange: import and export (ADP-17)

`src/workflows/interchange.py` — a canonical, versioned envelope for
round-tripping a definition, and an importer for external graphs that only
turns a recognized subset into something executable.

| Method | Path | Body | Returns |
| --- | --- | --- | --- |
| `POST` | `/api/workflows/export` | `{"definition": {...}, "layout"?: {node_id: {x,y}}, "design_only"?: [...], "provenance"?: {...}}` | `{"ok": true, "export": {...}}` |
| `GET` | `/api/workflows/runs/{run_id}/export` | — | `{"ok": true, "run_id": ..., "export": {...}}` — the definition version the run actually ran under |
| `POST` | `/api/workflows/import` | the payload itself (canonical envelope, or an aigraphstudio-shaped graph) | `{"ok": true, "definition": dict\|null, "design_only": [...], "rejected": [...], "executable": bool}` — always `200`, the same way `/validate` answers a bad definition with `ok: false` rather than a 4xx |

### Export: `{schema_version, definition, layout, design_only, provenance}`

`export_canonical(definition, layout=None, *, design_only=None,
provenance=None)` wraps `WorkflowDefinition.to_dict()` as-is. `layout`
(`node_id -> {x, y}`) keeps ONLY entries whose node id the definition
actually has and whose `x`/`y` are real numbers — an unknown id or a
non-numeric position is dropped, never passed through to a renderer.
`design_only`/`provenance` are opaque metadata this module never reads back
into anything executable. Because `WorkflowDefinition.parse`/`.to_dict()` do
all the real work, exporting and re-importing a definition cannot change its
`fingerprint()` — `tests/test_adp16_preflight_interchange.py::
test_export_canonical_round_trips_through_import_with_the_same_fingerprint`
pins this.

### Import: two accepted shapes, one validation path

`import_external(payload)` recognises:

1. **This module's own canonical envelope** (`schema_version` + `definition`)
   — parsed through `WorkflowDefinition.parse()` exactly as `/validate`
   would; a cycle or an unknown node type is refused (`executable: false`,
   `definition: null`), never guessed past.
2. **An aigraphstudio-shaped payload** (`schemaVersion: 1`, `nodes`,
   `edges`) — see `src/workflows/interchange.py`'s module docstring for the
   assumed JSON shape and why it is *assumed*, not verified: no fetch of
   `gcjordi/aigraphstudio`'s source was made this lote (COMUN: no external
   research), so the shape is this module's own reconstruction from the
   INFORME §6.2 node-type vocabulary, not a byte-for-byte match to the real
   exporter. Whoever wires a real aigraphstudio export up to this route
   should diff a real exported file against the module docstring's example
   first, and adjust `_looks_aigraphstudio`/the per-type translation if it
   differs.

Anything else — not an object, missing both `definition` and `schemaVersion:
1` — comes back as `executable: false` with a named reason, not a crash.

**Only six aigraphstudio node types become an executable Faustus node**,
because only these six have a real, checked handler in
`src/workflows/handlers.py::default_handlers`:

| aigraphstudio type | Faustus node type | Notes |
| --- | --- | --- |
| `Start` | `manual` | entry point |
| `Input` | `manual` | also a trigger; nothing in this schema formally declares "expected inputs" (see the preflight section above) |
| `Output` | `artifact_store` | records an output; still refuses to run until a store is wired (`artifact_handler`), same as any hand-written `artifact_store` node |
| `Tool` | `skill` | `config.skill` from `data.tool`/`data.skill`/the label |
| `Human Approval` | `human_approval` | direct match |
| `Router` | `condition` | ONLY when its branch is already a single `{left, op, right}` comparison `condition_handler` can evaluate (`data.condition` or `data.when`); anything else (multiple branches, free text, an unknown operator) stays `design_only` rather than being forced into a wrong single-branch translation — the INFORME's own limit: "no traducir router/merge por parecido visual" |

Every other named type — `Agent`, `LLM`, `Code`, `RAG`, `Memory`,
`Parallel`, `Merge`, `Loop`, `Retry`, `Evaluator` — is `design_only`
unconditionally: kept in the response for a human to see, never translated.

**Cascading demotion, so an unsupported upstream node can never produce a
false root.** If a node that WOULD otherwise map to an executable type has
an edge from a `design_only`/unknown/rejected node, it is demoted to
`design_only` too — and the demotion cascades multiple hops. The
alternative (dropping just that one edge, letting the downstream node become
a root with no `needs`) would run an effectful `skill`/`artifact_store` node
without whatever the source graph actually gated it on — exactly the
"apariencia de ejecución a lo que no se soporta" ADP-17 exists to refuse.
`tests/test_adp16_preflight_interchange.py::
test_import_aigraphstudio_cascades_design_only_instead_of_a_false_root`
covers a `Tool` node fed only by an `Agent` node: neither node makes it into
`definition`.

**A cycle among the surviving executable nodes is never habilitated**,
even though the source graph never called Faustus's cycle check itself —
the assembled candidate always goes through the real
`WorkflowDefinition.parse()`, `_find_cycle` included, never bypassed. An
unrecognized node type anywhere forces `executable: false` for the WHOLE
graph, even if the mapped subset would otherwise parse on its own — the
`rejected` entry says why either way.

## What this lot did NOT do

- No pricing catalogue was added anywhere. If a future lot exposes real
  OpenRouter/local pricing through `src/model_capabilities.py`, `estimate`'s
  `prices` parameter is the integration point — the estimator itself does not
  need to change, only whichever caller wires a live `prices` mapping in.
  `preflight`'s `prices` parameter is the same integration point.
- `lint_workflow` does not attempt to model `condition` branch semantics
  precisely (which nodes a specific condition outcome would actually skip);
  it only asks "is there ANY condition/human_approval upstream at all". The
  cost estimator's `condition`-gating is the same kind of approximation, in
  the direction of a min/max range rather than a single wrong number.
- `import_external`'s aigraphstudio shape is this module's own
  reconstruction, not verified against the real project's exporter (see
  above) — the risk is understated coverage (more of a real export landing
  in `design_only` than necessary), not a false "executable".
- No canvas/React Flow editor was added; `export_canonical`'s `layout` field
  is the integration point a future editor would read/write, unchanged by
  this lot.
