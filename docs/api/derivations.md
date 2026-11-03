# Learned recipes, derived results and the decision map

One dispatcher (`src/derivation_ops.py`) behind three surfaces: the chat tools `recipe_learn`, `derivation_graph` and
`decision_map`, the HTTP routes below and the built-in MCP server `derivations`. Data is per owner
(`DATA_DIR/recipes/<owner>`, `DATA_DIR/derivations/<owner>/<graph>.json`). Every HTTP route needs an administrator.
Nothing here runs a tool or a transformation by itself, except the explicit real execution of a recipe
(`action=execute`, below), which runs its steps through the normal tool dispatcher.

## Recipes (`src/recipe_learning.py`)

| Route | Body / query | Result |
| --- | --- | --- |
| `POST /api/recipes/learn` | one of `run_id`, `session_id` (+`turn`), `workflow_run_id`, or `request` + `steps: [{tool, args}]`; `title`, `params_hint` | `{recipe, next}` draft with `params`, `step_templates`, `examples`, `limits`, `source` |
| `POST /api/recipes/{id}/try` | `{params: {name: value}, mode?}` for a second case | default `mode=dry_run`: `{mode: "dry_run", kind: "structural_dry_run", executed: false, verified: false, resolved, total, steps, leaks, untested_params, promotable, outputs}`; `mode=execute` is the same as `/execute` |
| `POST /api/recipes/{id}/execute` | `{params, workspace, criteria?, inputs?, job_timeout?, rerun?}` | `{mode: "execute", status, executed, verified, steps, criteria, graph, ran_now, reused, skipped, why_unverified?}` |
| `GET /api/recipes/{id}/verification` | | `{exit_criteria, cases: [{params, status, verified, fresh, stale_reasons}]}` |
| `POST /api/recipes/{id}/promote` | | `{recipe, skill, promoted_from, verified_execution}`; 400 unless the last try was clean and the recipe did not change since |
| `GET /api/recipes/search` | `query`, `limit` | recipes matching, built-in, draft and promoted |

Parameters are typed `{name, kind: path|name|id|url|number, example, source}`. A step template is `{index, tool, args}`
where a whole-value parameter is `{"$param": name}` and one inside a text is `{{name}}`.

### Real execution (`src/recipe_execution.py`)

`execute` (chat/MCP: `recipe_learn {action: "execute"}` or `{action: "try", mode: "execute"}`) renders the recipe
like `try`, refuses (`status: not_runnable`) when a parameter is missing, a step is unresolved or a first-case
literal leaks, and otherwise runs each step through `code_mode.bridge.dispatch_call` (the `execute_tool_block` path)
with the caller's owner, workspace, session, security context and tool policy. No permission or confirmation is
added; a blocked call is a failed step. `workspace` defaults to the active workspace; over HTTP and MCP it must be
given, and inside a chat it must be inside the active one. `recipe_learn` itself is disabled inside a run.

- Steps are nodes of a derivation graph `recipe-run-<recipe>-<case hash>` (same owner), chained, with the case's
  existing input files as inputs (parameter values that are files, not counting checked deliverables, or `inputs`)
  and the recipe fingerprint as operation version. A completed step is not run again while its record matches
  (`status: reused`); an input file that changed or a changed recipe makes the steps stale. The node's
  idempotency key is the dispatcher's call id. `rerun: true` drops the graph first.
- Each step: `ok`, `error` (error object, non-zero exit, blocked, `ok:false`, process exited non-zero or stopped,
  outcome unknown), `queued` (still running after `job_timeout`, default 120 s, or a job id with no known waiter)
  or `exception`. `process_start` handles and `bg:` jobs are waited on with `process_read`. The first step that is
  not `ok` stops the chain; later steps are `skipped`. Each step reports `ids`, compact `output`, `why_run`.
- Exit criteria (stored on the recipe as `exit_criteria`; parameters with `{{name}}`), checked by opening the files
  after the steps: `file_exists {path}`, `contains {path, text}`, `regex {path, pattern, min_count}`,
  `row_count {path, equals|min|max}`, `columns {path, equals|includes}`,
  `number {path, pattern (one group), equals, tolerance}`. `equals` may be computed from another file:
  `{csv_sum: {path, column}}`, `{rows_of: path}`, `{columns_of: path}`. Tables are read from CSV/TSV, the first
  Markdown table of `.md`/`.txt` or the first HTML table.
- `status`: `verified` (every step `ok`/`reused`, at least one criterion, all passed), `criteria_failed`,
  `step_failed`, `incomplete` (a step `queued`), `executed_unverified` (no criteria), `not_runnable`, `busy`.
- Every run is stored on the recipe (`executions`, last 20) with the recipe fingerprint, criteria hash and the
  hashes of the input and checked files; `verification` re-measures them and marks a verification stale
  (`recipe_changed`, `criteria_changed`, `input_changed:<path>`, `delivered_file_changed:<path>`).
- `promote` is unchanged; it reports `promoted_from: verified_execution` when a fresh verification exists,
  otherwise the kind of the last try (`structural_dry_run`).

Example:

```json
{"action": "execute", "recipe_id": "draft-informe-de-ventas-1a2b3c4d", "workspace": "/path/to/project",
 "params": {"file": "ventas_sur.csv", "file_2": "informe_sur.md"},
 "criteria": [
   {"kind": "file_exists", "path": "{{file_2}}"},
   {"kind": "row_count", "path": "{{file_2}}", "equals": {"rows_of": "{{file}}"}},
   {"kind": "number", "path": "{{file_2}}", "pattern": "^Total: ([0-9.]+)$",
    "equals": {"csv_sum": {"path": "{{file}}", "column": "total"}}}]}
```

Limits: a step left running by a crash is run again and flagged `interrupted_rerun` (only the tool knows whether
its effect happened); values a tool returned are not passed to later steps; criteria are structural and numeric,
not a judgement of quality.

## Derivation graph (`src/derivation_graph.py`)

`POST /api/derivations/{graph}/{action}` with `action` one of `define`, `remove`, `rebuild`, `complete`, `base_dir`,
`atlas`; `GET /api/derivations`, `GET /api/derivations/{graph}`, `GET /api/derivations/{graph}/plan?target=`.

* `define`: `{node, op: {kind, name, version}, inputs: ["path" | {node} | {artifact_id}], params, outputs, base_dir}`.
* `plan`: nodes with `stale`, `conditional` and `reasons` (`input_changed:file:x`, `parameter_changed`,
  `tool_version_changed:1->2`, `output_changed:x`, `output_missing:x`, `never_run`, `previous_attempt_failed`,
  `interrupted`, `decision_map:...`).
* `rebuild {target?}`: `awaiting_execution` with `next` (`id`, `op`, `inputs`, `params`, `idempotency_key`, `interrupted`),
  or `up_to_date`. Run the node, then `complete {node, outputs: [paths], value?, error?}`; hashes are measured at that moment.
* `atlas {project_id, sync?}`: register finished results in Atlas's Hoard through the Hub; without a project or
  without an answer the result is `local_only: true` with the reason.

## Decision and effect map

`GET /api/decision-map/{graph}`, `GET /api/decision-map/{graph}/affected-by?change=&invalidate=`,
`POST /api/decision-map/{graph}/{add|link|unlink|remove}`. Item kinds: `requirement`, `decision`, `unit`, `format`,
`assumption`, `deliverable`; derivation nodes are `node:<id>`. `affected_by` follows explicit links and node inputs only.

## MCP

Server `derivations` (`mcp_servers/derivations_server.py`): `recipe_learn`, `derivation_graph`, `decision_map` with the
same arguments. `ODYSSEUS_MCP_DERIVATIONS_OWNER` names the owner; without it every call is refused.
