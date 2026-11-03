# Learned recipes, derived results and the decision map

One dispatcher (`src/derivation_ops.py`) behind three surfaces: the chat tools `recipe_learn`, `derivation_graph` and
`decision_map`, the HTTP routes below and the built-in MCP server `derivations`. Data is per owner
(`DATA_DIR/recipes/<owner>`, `DATA_DIR/derivations/<owner>/<graph>.json`). Every HTTP route needs an administrator.
Nothing here runs a tool or a transformation by itself.

## Recipes (`src/recipe_learning.py`)

| Route | Body / query | Result |
| --- | --- | --- |
| `POST /api/recipes/learn` | one of `run_id`, `session_id` (+`turn`), `workflow_run_id`, or `request` + `steps: [{tool, args}]`; `title`, `params_hint` | `{recipe, next}` draft with `params`, `step_templates`, `examples`, `limits`, `source` |
| `POST /api/recipes/{id}/try` | `{params: {name: value}}` for a second case | `{resolved, total, steps, leaks, untested_params, promotable, outputs, executed: false}` |
| `POST /api/recipes/{id}/promote` | | `{recipe, skill}`; 400 unless the last try was clean and the recipe did not change since |
| `GET /api/recipes/search` | `query`, `limit` | recipes matching, built-in, draft and promoted |

Parameters are typed `{name, kind: path|name|id|url|number, example, source}`. A step template is `{index, tool, args}`
where a whole-value parameter is `{"$param": name}` and one inside a text is `{{name}}`.

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
