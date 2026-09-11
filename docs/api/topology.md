# Topology: Mermaid, profile/workflow linting, cost estimate (Lote A4 + ADP-16/17)

`gcjordi/aigraphstudio` ported onto REAL Faustus data — no drawing-only model
of its own. Pure modules, wired onto existing routers:

- `src/topology_export.py` — `future_to_mermaid`, `workflow_to_mermaid`.
- `src/agent_profile_lint.py` — `lint_profile`, `lint_all`, `lint_workflow`,
  `find_cycles`.
- `src/workflow_cost_estimate.py` — `estimate`, `ModelPrice`, and (CMP-08,
  W2-B) `estimate_detailed`, `StructuredPrice`, `CallsProfile` — separate
  `node_activations`/`model_calls`/`external_ops`/`tokens` accounts, a
  structured/sourced price, and a `structural_bounds`/
  `forecast_with_assumptions`/`measured` split. See "Detailed estimate"
  below.
- `src/plan_compare.py` (CMP-08, W2-B) — `compare`: several candidate
  definitions for the same goal, one table with a `computed`/`estimated`/
  `unknown` tag per cell. See "Comparing plans" below.
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
| `POST` | `/api/workflows/simulate` | `{"definition": {...}, "choices"?: {node_id: bool}, "rounds_max"?: 1-200 (default 25)}` | `{"ok": true, "simulation": {...}}` |
| `POST` | `/api/workflows/estimate` | `{"definition": {...}, "prices"?: {model_id: {"prompt_usd_per_1k", "completion_usd_per_1k"}}}` | `{"ok": true, "estimate": {...}}` |
| `POST` | `/api/workflows/estimate?detail=1` (CMP-08) | same body, plus `"capability_pricing"?`, `"skill_calls_profiles"?`, `"local_latency"?`, `"run_id"?` | `{"ok": true, "estimate": {...}}` — `estimate_detailed()`'s shape, see "Detailed estimate" below |
| `POST` | `/api/workflows/compare-plans` (CMP-08) | `{"goal": str, "plans": [{"id", "label"?, "definition"}], "prices"?, "capability_pricing"?, "skill_calls_profiles"?, "local_latency"?}` | `{"ok": true, "comparison": {...}}` — see "Comparing plans" below |
| `POST` | `/api/workflows/preflight` | `{"definition": {...}, "prices"?: {...}, "installed_models"?: [...]}` | `{"ok": true, "preflight": {...}}` — now also carries `cost_detail` (CMP-08, see below) |
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

### Detailed estimate (`?detail=1`) — separate accounts (CMP-08)

`estimate()` above folds every node's activation count into one
`calls_min`/`calls_max` and prices a `skill` node as if it always makes
exactly one model call. Both are wrong for "how many model calls will this
make" and "will a three-call skill get undercounted". `estimate_detailed()`
(`src/workflow_cost_estimate.py`) answers those separately, and
`POST /api/workflows/estimate?detail=1` exposes it — same body as
`/estimate`, plus four optional inputs this pure function refuses to fetch
itself (no network in an estimate):

```json
POST /api/workflows/estimate?detail=1
{"definition": {...}, "prices"?: {...},
 "capability_pricing"?: {model_id: {"pricing": {"prompt": "0.000002", "completion": "0.000006"}, ...}},
 "skill_calls_profiles"?: {skill_id: {"model_calls", "external_ops", "tokens_in", "tokens_out"}},
 "local_latency"?: {model_id: {"load", "queue", "prefill_tps", "generation_tps", "memory_server"}},
 "run_id"?: "wfr_...",
 "owner"?: "...", "project_id"?: "..."}
```

`owner`/`project_id` (W3-C) are ONLY used to auto-fill `skill_calls_profiles`
for any skill declaring its own `calls_profile` in its `SKILL.md` — see the
follow-up section below. Omitting either keeps today's behaviour exactly.

```json
{
  "node_activations": {"min": 3, "max": 3},
  "model_calls": {"min": 1, "max": 1},
  "external_ops": {"min": 1, "max": 1},
  "tokens": {"in": {"min": 1500, "max": 1500}, "out": {"min": 500, "max": 500}},
  "cost_known_usd": {"min": 0.0025, "max": 0.0025},
  "cost_unestimable": [],
  "structural_bounds": {"node_activations": {"min": 3, "max": 3}, "note": "..."},
  "forecast_with_assumptions": {"node_activations": {"min": 3, "max": 3}, "model_calls": {...},
                                 "external_ops": {...}, "assumed_iterations": 3,
                                 "default_tokens_per_call": {"prompt": 1500, "completion": 500}},
  "measured": null,
  "prices_used": {"local:test-7b": {"amount_prompt_per_1m": 1000.0, "amount_completion_per_1m": 2000.0,
                                     "unit": "per_1M_tokens", "currency": "USD", "source": "caller_prices", "as_of": ""}},
  "per_node": [...],
  "assumptions": ["a 'skill' node with a bare config.model (no calls_profile) is assumed to make exactly 1 model call of 1500 prompt + 500 completion tokens"]
}
```

- **Separate accounts, not one blended number.** `node_activations` counts
  every node's activation, `model_calls` counts only `skill` activations
  that actually invoke a model, `external_ops` counts `deliver`/
  `artifact_store` activations AND a composite skill's own declared
  external operations (see below) — a `deliver` node is never counted as a
  model call, and a plain `wait`/`condition`/`human_approval` activation is
  never counted as either.
- **Price is always structured, never a bare number.**
  `{amount_prompt_per_1m, amount_completion_per_1m, unit: "per_1M_tokens",
  currency, source, as_of}`. `source` is one of `"caller_prices"` (the same
  `ModelPrice` shape `/estimate` already accepts, converted from per-1k to
  per-1M) or `"openrouter_pricing_field"` (read off
  `capability_pricing[model_id].pricing` — the caller's own already-fetched
  OpenRouter `/models` payload; this module never fetches a catalogue
  itself). `capability_pricing` wins over `prices` for a model ONLY when it
  actually names a usable `pricing` field — never assumed present.
- **A composite skill's real call count comes from a declared `calls_profile`
  or compatible history, never assumed to be "one call".** A `skill` node
  naming `config.skill` is looked up in `skill_calls_profiles` (declared,
  from that skill's own manifest — the caller reads it; this module never
  scans the filesystem), then in `DATA_DIR/skill_call_history.json` (this
  route DOES read that file itself — see `_skill_call_history_from_disk`)
  if `skill_calls_profiles` has nothing for it. Neither found →
  `calls_profile_source: "unknown"`, `model_calls: {min: 0, max: 0}`, and the
  skill is named in `cost_unestimable` — never defaulted to `1`. Two skill
  nodes naming the SAME model but different `config.skill` ids get different
  `model_calls`/`tokens_in`/`tokens_out` when their profiles differ. A plain
  `skill` node with only `config.model` (no `config.skill`) IS assumed to
  make one call — that assumption is named in `assumptions`.
- **`structural_bounds` / `forecast_with_assumptions` / `measured` are three
  different things.** `structural_bounds` is the graph shape alone — an
  unbounded cycle reports `max: "unbounded"` (the string), never a number.
  `forecast_with_assumptions` applies `assumed_iterations` to that same
  cycle and lists every assumption used. `measured` is `null` unless a real
  `run_id` is given, and even then only carries `tool_calls`/
  `active_seconds` (from `WorkflowStore.usage_so_far`) — `tokens_in`/
  `tokens_out`/`cost_usd` stay `null` with a note explaining why: no
  per-node token/cost ledger exists for a workflow run today (checked
  `src/workflows/engine.py` and `store.py` before writing this).
- **Local latency is threaded through, never computed here.** `local_latency`
  is `{model_id: {...}}` the CALLER already computed from
  `resource_admission.status()` (queue), `llm_core.local_speed()`
  (prefill/generation tok/s, learned per-model, `None` until observed),
  `vram_admission.reservations_snapshot()` (memory reserved per real
  server, never summed across servers) and `gpu_policy.model_sizes()`
  (on-disk size — the one of these four that is itself a network call,
  which is exactly why `estimate_detailed` never makes it). This function
  only attaches whatever it is given onto the matching `per_node` row's
  `latency_estimate`; an unset model's row has none, not a guessed number.
  W3-C adds the "caller" side of that sentence — see below.
- **`preflight()` now also carries `cost_detail`** — the same
  `estimate_detailed()` shape, computed with the SAME `prices`/
  `assumed_iterations` the preflight already used (edición mínima, no new
  input). `preflight()`'s own `cost` field is unchanged.

### Comparing plans — `plan_compare.compare` (CMP-08)

`POST /api/workflows/compare-plans` — several candidate definitions for the
same `goal` (typically `single_model`/`agent_team`/`deterministic_steps`,
but any ids), one table instead of diffing several `/estimate?detail=1`
responses by eye:

```json
{
  "goal": "answer a research question",
  "plan_ids": ["single_model", "agent_team", "deterministic_steps"],
  "plan_labels": {"single_model": "One model", ...},
  "rows": [
    {"metric": "model_calls_max", "cells": {
      "single_model": {"value": 1, "basis": "computed"},
      "agent_team": {"value": 2, "basis": "unknown"},
      "deterministic_steps": {"value": 0, "basis": "computed"}
    }},
    ...
  ],
  "reasons": {"agent_team": ["skill 'unknown_pack' has no declared calls_profile or compatible history — model_calls unknown"]},
  "detail": {"single_model": {...estimate_detailed()...}, ...},
  "errors": {}
}
```

- Every plan is estimated with the SAME pricing/profile/latency inputs, so
  none is priced more generously than another — this is the actual point of
  "por qué este plan" ("why this plan"): the table, not a verdict.
- **`basis` on every cell**: `"computed"` (the workflow's structure fixes
  it — no cycle, nothing unpriced/uncharacterized touches this metric),
  `"estimated"` (depends on a listed assumption — an unbounded cycle's
  `assumed_iterations`), or `"unknown"` (the plan touches an unpriced model
  or an uncharacterized composite skill — reported, never hidden).
  `plan_compare.py` adds no estimation logic of its own; every number comes
  from `estimate_detailed`.
- A plan whose `definition` fails to parse is reported in `errors` and kept
  out of `rows`/`detail` — one bad plan never hides the other two.
- Nothing here ranks or picks a plan; that is left to whoever reads the
  table.

### Follow-up (W3-C, `CONTRATO_W3.md`, CMP-08 seguimiento): declared profiles, call history, local latency, and the Studio UI

Four gaps the W2-B lot above left open on purpose (documented as such at the
time) closed here — no change to `estimate()`/`estimate_detailed()`'s own
signature or to `/estimate`'s/`/compare-plans`' response shape, only to
where their optional inputs come from and how the Studio surfaces the
result:

- **A skill can declare its own `calls_profile` in its `SKILL.md`.**
  `SkillManifest` (`src/contracts/skill.py`) gained an optional
  `calls_profile: CallsProfileSpec | None` field (`{model_calls,
  external_ops, tokens_in, tokens_out}`, every count a non-negative number —
  a fractional amortised average is legitimate, unlike the rest of this
  contract's whole-number fields). `src/skills_runtime/bridge.py` reads it
  from the same flat-key frontmatter shape `permissions_*` already uses
  (this parser has no nested maps): `calls_profile_model_calls: 3`,
  `calls_profile_external_ops: 1`, `calls_profile_tokens_in: 4200`,
  `calls_profile_tokens_out: 900` in a `SKILL.md`'s frontmatter (or a
  `calls_profile:` mapping, for callers building frontmatter as a dict).
  Declaring none of the four keys leaves `calls_profile: None` — never a
  zeroed-out profile.
  `POST /api/workflows/estimate?detail=1` and `/compare-plans` now pick
  this up automatically for every `skill` node's `config.skill` in the
  definition, when the request body also names `owner`/`project_id`
  (`routes.workflows_routes._declared_calls_profiles_from_manifests`,
  walking that project's workspace with `src.skills_runtime.discovery` the
  same way `src/workflows/skills.py::run` finds a script skill to execute).
  An explicit `skill_calls_profiles` entry in the request body still wins
  over the manifest for any skill named in both — this is a convenience
  default, never a requirement. `/estimate?detail=1`'s body gains two
  optional fields: `"owner"?: string, "project_id"?: string`.
  `/compare-plans` has no single `definition` to walk (several plans, no
  canonical one) and does not pick this up — only `skill_calls_profiles`
  from the body and `skill_call_history.json` apply there, as before.

- **`DATA_DIR/skill_call_history.json` is now actually written.** Every
  time a workflow's `skill` node finishes running a script skill
  (`src/workflows/skills.py::run`, not refused), it folds the run into
  `{skill_id: {runs, model_calls?, external_ops?, tokens_in?, tokens_out?}}`
  under a cross-process `core.kernel_file_lock.KernelFileLock` (same idiom
  `src/workflows/credentials.py` uses for its own read-modify-write store),
  written with `core.atomic_io.atomic_write_json`. `runs` is always known
  and always counted. The four counts are folded in as a running average
  over the runs that DID report them (`<key>_samples`, an internal
  bookkeeping field) — **only when this call is actually given a real
  number for that count**, which today it never is: a script skill runs in
  a container and this handler has no signal for its own model/token
  usage. So in practice every entry written today is `{"runs": N}` and
  nothing else, honestly reflecting "we know it ran N times and nothing
  about its call shape".
  `routes.workflows_routes._skill_call_history_from_disk` — CMP-08's
  second, fallback source of a `calls_profile` — now **drops any entry
  that never gained a real count**, rather than handing `{"runs": N}`
  straight to `workflow_cost_estimate.calls_profile_from_mapping`, which
  reads a missing key as `0` via `.get(key, 0)`. Passing that through
  unfiltered would have turned "we do not know this skill's call shape"
  into "this skill makes 0 model calls" — exactly the `unknown`-read-as-
  `0`/`free` mistake CMP-08 exists to refuse. A skill with only a `runs`
  count keeps falling through to `calls_profile_source: "unknown"`, same
  as if the history file had never mentioned it.

- **`local_latency` can now actually be computed, in a NEW pair of
  functions kept OUT of `estimate()`/`estimate_detailed()` on purpose** —
  both stay pure and network-free, exactly as before.
  `src.workflow_cost_estimate.local_latency_for(model, *, endpoint_url="")`
  returns one `{"load", "queue", "prefill_tps", "generation_tps",
  "memory_server"}` row (plus `size_bytes` when known), every field
  `"unknown"` unless a real signal names it:
  - `generation_tps` — `src.llm_core.local_speed(model)`, the decode speed
    Faustus has itself learned from that model's own replies this
    process's lifetime. `"unknown"` until the model has actually replied
    once.
  - `load` — always `"unknown"`. `src.gpu_policy.model_sizes(endpoint_url)`
    gives the model's size on disk (surfaced as `size_bytes` when the
    lookup succeeds), but nothing in this codebase measures how long
    loading that many bytes takes — `llm_core`'s only local timing table is
    decode speed, never load time. Per this lot's contract: report
    `unknown` rather than guess a load time from size and an assumed
    disk/PCIe throughput.
  - `queue` — `src.resource_admission.status()`'s live counters
    (`{pool_id, available, foreground_waiting}`) for whichever pool
    `endpoint_url` belongs to (`resource_admission.pool_for_endpoint`);
    `"unknown"` when the endpoint is in no pool (nothing is queuing there,
    by construction of that module) or no `endpoint_url` was given.
  - `prefill_tps`, `memory_server` — always `"unknown"`: neither
    `gpu_policy`, `llm_core` nor `resource_admission` (the three sources
    this function is scoped to) expose a prompt-processing rate or a
    per-server VRAM reservation figure; `src.vram_admission` has the
    latter, but reading it is a separate lot's scope.
  `local_latency_snapshot({model_id: endpoint_url})` batches this over
  several models into exactly the mapping `estimate_detailed(...,
  local_latency=...)` expects.
  **Wired into `/estimate?detail=1` (W3-INT).** When the request body does
  not already carry `local_latency`, `_detail_inputs_from_payload` now calls
  `_local_latency_snapshot_for_definition(definition)`, which walks the
  definition's own `skill` nodes and calls `local_latency_snapshot` with
  `endpoint_url=""` for each named model (no endpoint is known at the route
  layer, so `load`/`queue` still read `"unknown"` there — only
  `generation_tps`, keyed on model alone via `llm_core.local_speed`, can
  come back populated this way). Never fails the route: any exception
  (`_local_latency_snapshot_for_definition`'s own try/except) degrades to
  `{}`, i.e. exactly the same as a caller sending no `local_latency` at all.
  A caller with real endpoint URLs can still pass its own `local_latency` in
  the body — that always wins, the fallback only fires when the field is
  absent.

- **Studio: `EstimateView.tsx` now shows CMP-08's separated accounts.**
  `WorkflowEstimateView` gained two OPTIONAL props — `definition` and
  `runId` — so `Activity.tsx` (a file this lot does not own) keeps
  compiling and rendering exactly what it renders today with zero edits.
  When a caller passes `definition`, the view fetches
  `workflowEstimateDetailed` and renders: node activations vs. model calls
  vs. external operations vs. tokens as separate rows (never one blended
  number), every listed assumption and every `cost_unestimable` reason (so
  an incomplete total reads as incomplete), a per-node breakdown where a
  `calls_profile_source: "unknown"` row shows "desconocido" for its model
  calls/tokens instead of `0`, "previsto vs real" once `measured` is
  non-null, and a **"Comparar planes"** button. That button builds two
  heuristic variants of the SAME graph client-side — `single_model` (every
  `skill` node's `config.model` overridden to whichever model the plan
  already names most often) and `deterministic_steps` (every `skill` node
  turned into an empty `manual` node) — and calls
  `POST /api/workflows/compare-plans` with `{current, single_model,
  deterministic_steps}`, rendering the result as a table with each cell
  tagged `computed`/`estimated`/`unknown` (`.fs-estimate__basis[data-basis]`).
  **Wired (W3-INT).** `Activity.tsx`'s `openEstimate` now also stores the
  resolved definition (`estimateDefinition`, alongside the existing
  `estimateResult`/`estimateFor` state) and the estimate dialog passes both
  `definition={estimateDefinition}` and `runId={estimateFor}` to
  `<WorkflowEstimateView>` — the detailed-accounts section and "Comparar
  planes" now render for real the first time a user opens "Estimate cost"
  on a run, not just under `studio/checks/w3c_estimate_followups.check.mjs`.

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

## Simulate: a structural walk with nothing executed (CMP-07)

`src/workflows/simulate.py::simulate(definition, *, choices=None,
rounds_max=25)` returns a `SimulationResult` — a round-by-round structural
walk of `definition`'s `needs` graph. It shares nothing with
`WorkflowEngine.advance` (the real run): no handler from
`src/workflows/handlers.py` is ever imported or called, no `WorkflowStore`
is opened, no network of any kind. It exists to answer, before anyone
authorizes a real run: which nodes would activate, where a run would stop
for a human, and which branches would not be taken.

```
POST /api/workflows/simulate
{"definition": {...}, "choices": {"gate": true, "approve": false}, "rounds_max": 25}
```

```json
{
  "ok": true,
  "simulation": {
    "rounds": [
      {"round": 1, "activated": ["start"], "not_taken": [], "human_waits": [], "newly_awaiting_choice": []},
      {"round": 2, "activated": ["audit", "gate"], "not_taken": [], "human_waits": [], "newly_awaiting_choice": []},
      {"round": 3, "activated": [], "not_taken": ["approve"], "human_waits": ["approve"], "newly_awaiting_choice": []},
      {"round": 4, "activated": [], "not_taken": ["send"], "human_waits": [], "newly_awaiting_choice": []}
    ],
    "activated": ["audit", "gate", "start"],
    "not_taken": ["approve", "send"],
    "human_waits": ["approve"],
    "awaiting_choice": {},
    "not_reached": [],
    "rounds_used": 4,
    "rounds_max": 25,
    "warnings": ["`needs` are AND dependencies: ..."]
  }
}
```

### `choices`: a guess the caller supplies, never a computation

`simulate()` never evaluates a `condition` node's real `config.when`
against live data (that is `handlers.py::evaluate`'s job, against a real
run's inputs) and never asks a real person to answer a `human_approval`
node. `choices: {node_id: bool}` lets a caller explore one branch at a
time: `true` assumes that `condition` passes or that `human_approval` is
granted; `false` assumes it fails or is denied. A `condition`/
`human_approval` node with **no** entry in `choices` is left **undecided**
— reported in `awaiting_choice`, together with every node downstream of it
(each pointing back at the SAME blocking node id, not at its own
immediate, also-undecided parent) — never silently assumed to pass. This
is the direct answer to INFORME §3.6's own limit: a simulator that
defaulted an unanswered condition to "passes" would be lying about
authorizing a plan it never actually checked.

`human_waits` always lists a `human_approval` node once it is reached,
whether or not `choices` supplied a guess for it — a real run always
pauses there regardless of what a caller is choosing to explore past it in
one simulation call.

### `needs` are AND dependencies, never control branches

This is INFORME §3.6's central warning, and `SimulationResult.warnings`
states it on every call: a node with several `needs` only activates once
**every** one of them is satisfied. This schema has exactly one dependency
mechanism (`needs`) and no separate "true"/"false" edge — so a node
downstream of an undecided or denied gate never activates just because
some *other* path in a drawn diagram appears to reach it; `simulate()`
respects `needs` exactly as `WorkflowEngine.ready_nodes`/`_stopping` would
at run time. `tests/test_cmp07_simulate.py::
test_and_dependencies_are_never_bypassed_by_a_second_path` pins a node
with two `needs` — one clean, one gated by an undecided `condition` — and
confirms it does not activate on the strength of the clean one alone.

### Rounds are depth layers, not an arbitrary loop counter

Each round only resolves nodes whose dependencies were **all** settled by
the end of the PREVIOUS round; a node whose dependency resolved earlier in
the SAME round still waits for the next one. `rounds_max` (1–200, default
25) is therefore a real cap on how many dependency layers deep the walk
goes — the same number `PlanGraph`'s depth-layered rendering (Studio, this
lot) keys off — and hitting it is reported in `not_reached`/`warnings`,
never silent.

### Why a cyclic definition never reaches this module

`WorkflowDefinition.parse()` already refuses every cycle
(`src/contracts/workflow.py::_find_cycle`, untouched by this lot); `POST
/api/workflows/simulate` reuses the same `_definition_or_400` every other
`/api/workflows/*` route does, so a cyclic payload is refused with the
usual `{path, reason, got}` message before `simulate()` ever runs. A
hand-built `WorkflowDefinition` that bypasses `.parse()` (the same
defensive case `agent_profile_lint.find_cycles` exists for) does not crash
`simulate()` either — its cyclic nodes simply never resolve and are
reported in `not_reached` once `rounds_max` is spent, which is an honest
answer rather than an infinite loop.

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
