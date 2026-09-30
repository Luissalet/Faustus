# Workflows: model-driven nodes, tools, evaluation and templates

Workflow definitions (`src/contracts/workflow.py`) are a flat, acyclic graph:
every node lists the nodes it `needs`, and the engine (`src/workflows/engine.py`)
claims, runs and records one node at a time, keeping a row per node so a
restarted process reads what already happened instead of doing it again.

This document covers what was added on top of that graph so a workflow can
decide things, repeat a few steps, be offered to other programs as a tool and be
checked against cases. The node contracts are in `src/contracts/`, the
handlers in `src/workflows/model_nodes.py` and `src/workflows/loop.py`, the
routes in `routes/workflows_routes.py`, and the Studio half is the Workflows
screen (`studio/src/screens/workflows/`).

All routes below need an admin session or an API token; the `/published` routes
need a token with the `agents:dispatch` scope. Everything is scoped to the
calling owner: one owner never sees another's saved workflows, sets, reports
or published tools.

## Node types

| Type | What it does | Reaches outside Faustus |
| --- | --- | --- |
| `agent` | One headless agent turn: a prompt, an allow-list of tools, an optional output schema | Yes, through the tools it may call |
| `classify` | Chooses exactly one of several declared labels and records how sure it was | No, it only reads |
| `extract` | Pulls the fields of a JSON schema out of a text | No, it only reads |
| `guard` | Deterministic checks plus optional yes/no model checks; two branches, `pass` and `fail` | No, it only reads |
| `loop` | Repeats a few sibling nodes, bounded, until a condition holds | Only through what its body does |

A node that uses a model never pretends to. Each handler is built from a seam
(an agent runner, a model client). With nothing wired it fails by name
("no model is wired to the 'classify' node type; nothing ran") instead of
choosing a branch at random, and the failure is recorded on the node.

### Templates in config

Text fields are templates. `{{ inputs.ticket }}` reads a run input and
`{{ results.fetch.text }}` reads an upstream node's result; dotted paths only,
no expressions and no calls. A reference to something that does not exist is an
error, never an empty string, so a model is never asked a question about
nothing. `{{ path | json }}` renders a value as JSON.

### `agent`

| Field | Meaning |
| --- | --- |
| `prompt` (required) | Template; what the agent is asked. Up to 100 000 characters |
| `system` | Template; extra system instructions |
| `agent` | Name of an agent profile; it contributes its prompt and its tool permissions |
| `tools` | Absent: the agent default. `[]`: think only. A list: exactly these tools, nothing else |
| `output_schema` | JSON schema. The reply is parsed and checked; one repair attempt, then the node fails |
| `max_rounds` | 1 to 50, default 8 |
| `timeout_s` | 5 to 3600, default 300 |
| `model` | Optional model name |

Result: `text`, `rounds`, `tool_calls`, `model`, `profile`, `session_id`,
`tool_events` (first 40), `stop_reason`, and, with an `output_schema`, `data`
and `repaired`.

An agent turn cannot answer an approval card mid-turn. If a tool asks for
approval the node fails with that reason; do that step in a `skill` or
`deliver` node behind a `human_approval` node, or allow the tool beforehand.

### `classify`

| Field | Meaning |
| --- | --- |
| `text` (required) | Template; what to classify |
| `labels` (required) | 2 to 20 labels, each a string or `{name, description}`; unique |
| `question`, `instructions` | Optional wording for the model |
| `threshold` | 0 to 1, default 0.7: the confidence the choice must reach |
| `fallback` | One of the labels: the route taken when unsure |
| `on_uncertain` | `fallback` (default when a fallback is set) or `ask` (ask the model once more in plain words) |
| `timeout_s` | 1 to 600 |

The choice is read as a probability, so it comes with a confidence. Below the
threshold, or when the server gives no usable answer, the node takes the
fallback (or asks once more); if that settles nothing and there is no fallback
it fails rather than guess. Result: `{branch, label, receipt}`; the receipt
lists the options, the choice, the confidence, whether the fallback fired
(`fallback_used`) and which model answered. "Uncertain" in the Studio is the
route that goes through the fallback label.

### `extract`

`text` (required template), `schema` (required, `"type": "object"`),
`instructions`, `timeout_s` (1 to 600, default 60). The answer is validated
against the schema with one repair attempt. Input longer than 20 000 characters
is cut and the result says so (`input_truncated`). Result: `{data, repaired,
input_truncated}`. A field the text does not give is left out, not invented.

### `guard`

`text` (required template), `checks` (required, 1 to 12), `on_unknown`
(`fail` by default, or `pass`), `timeout_s`. Each check is a type name or an
object with that `type`:

* `secrets`: credential-shaped strings.
* `pii`: `kinds` from `EMAIL`, `PHONE`, `IBAN`, `CARD`, `ID`, `IP` (default the
  first three); IBAN and card numbers are validated before they count.
* `urls`: an `allow` and/or `deny` list of hosts (`*.host` matches subdomains
  only). One of the two is required.
* `injection`: instruction-hijack phrases.
* `model`: a yes/no `question` whose answer "yes" means the text is not
  acceptable, and a `threshold` the model's confidence must reach.

A guard is a gate, so it fails closed: a model check that cannot be settled
counts as a failure unless `on_unknown` is `pass`. The result never contains
the matched secret or address, only what kind it was and where. Branches:
`pass` and `fail`.

### `loop`

```json
{"id": "refine", "type": "loop", "needs": ["draft"],
 "config": {"body": ["review", "revise"],
            "budget": {"max_iterations": 4, "max_seconds": 600,
                       "max_tool_calls": 20, "on_exhausted": "pause"},
            "until": {"left": "results.review.data.ok", "op": "truthy"}}}
```

The engine still sees an acyclic graph; a loop is one node that runs a fixed
set of its siblings again. The body nodes are listed in `config.body`, are
never scheduled at the top level, may only be `agent`, `classify`, `extract`,
`guard`, `condition`, `artifact_store`, `skill` or `deliver`, and may only
depend on each other and on what the loop itself waits for. Nothing outside the
loop may wait on a body node, only on the loop, whose result carries the last
pass's outputs. A node belongs to at most one loop.

* `max_iterations` is required and at least 1; there is no unbounded loop.
* `max_seconds` counts active time (a pass waiting on a person does not count)
  and `max_tool_calls` counts effectful body nodes plus what agent turns
  report. `max_tokens` is refused: tokens are not metered per pass.
* `until` uses the same `left`/`op`/`right` shape as a `condition`; the
  operators are `eq ne gt gte lt lte contains in exists truthy`. It is checked
  after each pass.
* With an `until`, running out of a limit is "exhausted": `on_exhausted` is
  `pause` (default; a person extends the loop or stops the run) or `fail`.
  Without an `until`, reaching `max_iterations` is the loop's normal end.

One row per pass is kept (`workflow_iteration_runs`), opened before the pass
starts and updated as each body node settles. `GET /api/workflows/runs/{id}`
returns them under `loops`. A pass is resumed at the body node it was on;
finished passes are never run again.

## Branches

A node may be gated on what a `classify` or `guard` it needs decided:

```json
{"id": "refund_reply", "type": "agent", "needs": ["route"],
 "branch": {"route": ["refund", "billing"]}, "config": {"prompt": "..."}}
```

`branch` maps a dependency to the labels it must have chosen. The definition is
refused if the dependency is not in `needs`, is not a `classify`/`guard`, or
the label is not one it declares (`config.labels`, or `pass`/`fail`). A node
whose gate did not open is recorded as `skipped`, and whatever depends on a
skipped node does not run, because a skipped node produced no result. A gated
node that also waits on a non-branching node still waits for it.

## Idempotency and effects

Every node run has an idempotency key built from the run, the node, the
definition fingerprint and the node's config, so changing what a node does
changes its key.

* `classify`, `extract` and `guard` only read; a retry is merely another model
  call.
* An `agent` turn may call tools, so it runs behind the same protocol as a
  `skill`: the effect is marked `pending` before the turn starts and
  `confirmed` when it returns. A worker that dies in between leaves the node
  in `unknown_effect`; the engine does not start a second turn on its own. A
  turn that times out, is cancelled or errors leaves the effect `pending` for
  the same reason.
* Inside a loop the key also carries the pass number, so pass 3 and pass 4 of
  an effectful body node are different effects, while two attempts at pass 3
  are the same one. A pass found open with a `pending` effect fails that pass
  with `unknown_effect` and is not retried.
* A node that reaches outside Faustus may only have `max_attempts` above 1 when
  its config says `idempotent: true`.

## Validation, planning and simulation

* `POST /api/workflows/validate`, `/plan`, `/simulate`, `/preflight` and
  `/estimate` understand the new types. Preflight lints loops (`LINT-WF-LOOP-NO-UNTIL`,
  `LINT-WF-LOOP-EFFECT-UNMETERED`) and counts a loop body as running when the
  loop does. The cost estimate reports model calls per node (fewest and most),
  multiplied by a loop's bounds, and lists them as unpriced because the model is
  resolved at run time.
* `POST /api/workflows/dry-run` with `{definition, inputs, mocks}` walks the
  workflow with nothing real behind it: no model, skill, sender, store or
  clock. Each node type returns a deterministic placeholder that satisfies the
  schema the real node promises; `condition` and `loop` are really evaluated;
  templates are rendered for real, so a dangling reference fails here with the
  real message. `mocks: {node_id: {...}}` overrides one node (a classify's
  `label`, a guard's `passed`, an extract's `data`, or `status: "failed"` /
  `"skipped"`). A placeholder proves wiring, not model quality.
* Interchange (`/export`, `/import`) translates agent, model, evaluator and loop
  nodes; code, retrieval, memory, parallel, merge and retry nodes stay
  design-only and are reported as such, never silently dropped.

## Templates

`GET /api/workflows/templates` lists starting points with parameters.
`POST /api/workflows/templates/{id}/instantiate` with `{parameters, save?, name?}`
fills one in and returns the validated definition (`400` names every problem at
once). Nothing runs and nothing is published by instantiating.

| Id | Shape |
| --- | --- |
| `pdf-folder-batch` | A `wait_for_event` node (`source: file_change`) on a watched folder, an `agent` node restricted to the `pdf_ops` tool, and an `artifact_store` node for the report. Declares one input, `operations`. Files already in the folder when the run starts are not new files |
| `triage-and-reply` | `classify` into two categories, an `agent` draft per category, a `guard` on each draft, then store the draft or hold it for a person |
| `refine-until-good` | A bounded `loop` around an agent draft and a guard check, exiting when the check passes; at the ceiling it pauses for a person |

If `pdf_ops` is configured to ask for approval, the agent turn stops and the
run fails; allow it for the folder first.

## Publishing saved workflows as MCP tools

A workflow is kept in the library under a name, and may be published as a tool.
Saving never publishes; publishing is a separate switch, off by default, and
only possible when the definition declares `inputs` (a JSON schema with
`"type": "object"`).

| Route | Purpose |
| --- | --- |
| `GET /api/workflows/library` | The owner's saved workflows |
| `POST /api/workflows/library` | `{definition, name?, enabled?, allow_overrides?}` |
| `GET` / `PATCH` / `DELETE /api/workflows/library/{name}` | Read, switch `enabled` / `allow_overrides`, delete |
| `GET /api/workflows/published` | The tools enabled workflows publish |
| `POST /api/workflows/published/{tool}/call` | Start a run: `{arguments}` |
| `GET /api/workflows/published/runs/{run_id}` | A run's status and result |

A library holds at most 200 workflows per owner.

### The tool

The tool is named `wf_<name>`, described from the workflow, and its arguments
are the declared `inputs`, checked against the schema before anything starts,
plus three reserved arguments that no workflow input may be called:

* `overrides`: `{node_id: {field: value}}`, per-run changes to a few node
  settings. The tool's own schema lists exactly which fields each node accepts.
* `wait_seconds`: how long to wait for the run to finish before answering with
  its run id (default 30, at most 300). The run goes on either way.
* `idempotency_key`: calling again with the same key returns the run the first
  call started (`duplicate: true`).

Overridable fields, and nothing else: `agent` (`prompt`, `system`, `max_rounds`,
`timeout_s`, `output_schema`), `classify` (`threshold`, `fallback`,
`on_uncertain`, `instructions`, `question`, `timeout_s`), `extract`
(`instructions`, `schema`, `timeout_s`), `guard` (`on_unknown`, `instructions`,
`timeout_s`) and `loop` (`max_iterations`, `max_seconds`, `max_tool_calls`,
which can only be lowered). Tools, agent profiles, labels, checks, skills,
recipients and backends can never be overridden: they decide what a workflow
may touch. The owner can switch overrides off per workflow with
`allow_overrides: false`; a call that sends them anyway is refused. The
overridden definition is what the run snapshots, so what ran is visible in the
run.

A call starts a run on a background pool and answers with the run id and, when
the run finished or stopped for a person within the wait, its result. A second
tool, `workflow_run_status`, reads any run by id.

### The MCP server

`mcp_servers/workflows_server.py` is an external stdio server, like
`workers_server.py`: it is an HTTP client of the running Faustus, nothing runs
in its process, and its tool list is data that changes whenever a workflow is
saved, enabled or disabled. Add it to a client's MCP configuration:

```json
{
  "mcpServers": {
    "faustus-workflows": {
      "command": "<path to the Faustus Python>",
      "args": ["<Faustus folder>/mcp_servers/workflows_server.py"],
      "env": {"FAUSTUS_URL": "http://127.0.0.1:7000",
              "FAUSTUS_API_TOKEN": "<API token with the agents:dispatch scope>"}
    }
  }
}
```

If Faustus cannot be reached while listing, the list is just
`workflow_run_status`; listing again later picks the workflows up.

`tools/list` (abridged) for a saved workflow `triage` that declares a `ticket`
input:

```json
{"name": "wf_triage",
 "description": "Route a support ticket and draft a reply.",
 "inputSchema": {"type": "object", "required": ["ticket"],
   "properties": {
     "ticket": {"type": "string"},
     "overrides": {"type": "object", "additionalProperties": false,
       "properties": {"route": {"type": "object", "properties": {
         "threshold": {"type": "number", "minimum": 0, "maximum": 1}}}}},
     "wait_seconds": {"type": "number"},
     "idempotency_key": {"type": "string"}}}}
```

`tools/call`:

```json
{"name": "wf_triage",
 "arguments": {"ticket": "I was charged twice",
               "overrides": {"route": {"threshold": 0.8}},
               "wait_seconds": 60, "idempotency_key": "ticket-1042"}}
```

The answer is a JSON text with `run_id`, `finished`, `status`,
`overrides_applied` and the compact run (node states and the outputs of the
nodes nothing else consumes). Refusals carry a stable code: `unknown_tool`
(404), `bad_arguments` (400, with every problem listed), `bad_overrides` (400)
and `overrides_disabled` (403).

## Evaluation

An evaluation answers "does this saved workflow still do what it should?". A
set is a list of cases (inputs, optional mocks, optional scorers) plus
scorers that apply to every case.

| Route | Purpose |
| --- | --- |
| `GET /api/workflows/library/{name}/eval-sets` | Sets, and whether the model judge is enabled |
| `GET` / `PUT` / `DELETE /api/workflows/library/{name}/eval-sets/{set}` | Read, create or replace, delete a set |
| `POST /api/workflows/library/{name}/evaluate` | Run a set |
| `GET /api/workflows/library/{name}/evaluations` | Past reports |
| `GET /api/workflows/evaluations/{id}` | One report |

```json
{"scorers": [{"type": "exact", "path": "reply.data.category", "expected": "billing"}],
 "cases": [{"id": "double-charge", "inputs": {"ticket": "charged twice"},
            "mocks": {"route": {"label": "billing"}}}]}
```

Set names are lower case letters, digits, `-` and `_` (up to 48 characters).

**Scorers** read a `path` into the run's outputs (`{node_id: result}` for the
nodes nothing else consumes; no path scores the whole object): `exact`,
`contains`, `regex` (`pattern`), `json_schema` (`schema`), `numeric`
(`expected`, `tolerance`, `relative`) and `judge` (a model grades the output
against `criteria`). `judge` is the one non-deterministic scorer and is off by
default: with the setting `workflow_eval_model_judge` off it reports itself
unavailable and the case fails rather than passing on nothing.

**Modes.** `evaluate` takes `{set, mode, allow_real?, cases?, timeout_s?,
wait_seconds?}`.

* `simulate` (default): every case goes through the dry run; no model, skill
  or sender is touched. It proves the wiring, the schemas and the branches.
  The report says `mode: simulate` so it is never read as a statement about
  model quality.
* `real`: every case starts a real run of the saved definition and waits for
  it. It needs `mode: "real"` and `allow_real: true`, a case that stops for a
  person ends as `error`, and there are fewer cases allowed.

A case passes when the run completed and every scorer passed; a case without
scorers passes when the run completed and is counted as unscored. The report
has the pass rate, counts, average and slowest-5% duration, the pass rate per
scorer type and, per case, each score with its detail (for example
`expected "refund", got "Refund"`). If the evaluation is still running when
`wait_seconds` (0 to 120, default 20) ends, the answer has `finished: false`
and the report is read back by id.

## Limits

| Limit | Value |
| --- | --- |
| Saved workflows per owner | 200 |
| Evaluation sets per workflow | 20 |
| Cases per set | 100 (20 in real mode) |
| Scorers per case | 10 |
| Case timeout | 120 s default, 900 s at most |
| Classify labels | 2 to 20 |
| Guard checks | 12 |
| Extract input | 20 000 characters |
| Guard input | 200 000 characters |
| Rendered prompt | 200 000 characters |
| Published call wait | 30 s default, 300 s at most |

## Settings

| Setting | Default | Effect |
| --- | --- | --- |
| `workflow_eval_model_judge` | off | Lets the `judge` scorer call a model. Off: a judge scorer fails with "unavailable" |

API tokens that publish workflows to an outside client need the
`agents:dispatch` scope; the `/api/workflows/published` routes require it.

## The Workflows screen

`/workflows` has four tabs over the same definition:

* **Design**: a palette (Model-driven, Flow, Effects) adds nodes; a loop is
  added with one body step. "New blank workflow" and the template picker start
  a plan. Selecting a node opens its inspector: title, `needs`, the branch it
  runs on, a form for its config and the raw JSON, which stays in step with the
  form. Edges carry the option or `pass`/`fail` they are gated on; the classify
  fallback reads "(uncertain)"; loop bodies are drawn dashed under their loop.
  Applying a change re-runs preflight and lint. "Build, save and publish" holds
  the library: the inputs schema, save, the publish switch and overrides
  switch per workflow, and, per tool, its name, arguments, per-run overrides
  and a client configuration.
* **Structural simulation**: the existing choice-by-choice walk.
* **Authorized real execution**: starting a real run is one confirmed action.
  The graph then colours each node, marks the edge a classify or guard sent the
  run down, lists what each loop pass did (steps, exit condition, seconds) and
  refreshes itself while the run is going.
* **Evaluate**: edits sets, scorers and cases of the saved workflow and runs
  them. Simulate is selected; real needs the mode and a ticked confirmation.
