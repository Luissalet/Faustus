# Tool selection & connector policy (CONTRATO_CONECTORES Lote F2)

Fase E: per-task/project/session connector allowlists, enforced in the real
dispatcher (not just hidden in the prompt), plus an honest tool-support
signal. F2 does not touch `src/connectors.py` / `src/connector_sidecar.py` /
`routes/connector_routes.py` (Lote F1) — this lot's policy works purely off
`McpServer.id`, already present in `core/database.py` before F1 or F2 existed.

## §Trace

Read before writing a line of F2 code, as required by the contract. Line
numbers are from this worktree at the time F2 was implemented; they will
drift as the file changes, but the function names are stable anchors.

* **System prompt / schema assembly** — `src/agent_loop.py::_build_system_prompt`
  (def at line 3014). It calls `mcp_mgr.get_all_openai_schemas(mcp_disabled_map
  or {})` at (was) line 3082 to get every MCP tool's OpenAI-style schema, then
  hands the result (plus the RAG-narrowed `relevant_tools` set and the base
  prompt from `_build_base_prompt`) back to its caller. F2.3's schema filter
  is inserted immediately after that call (see §Enforcement below).
* **Per-server disabled-tool map** — `_load_mcp_disabled_map` (line 381)
  reads `McpServer.disabled_tools` (a JSON array of tool *names* to hide,
  admin-set per server) into `{server_id: {tool_name, ...}}`. This is a
  different axis from F2's connector allowlist: it hides individual tools
  *within* an otherwise-reachable server, never a whole server. F2.3 never
  touches this map — it filters the schema list `get_all_openai_schemas`
  already produced, one layer downstream.
* **Tool-RAG** — `src/tool_index.py`. `ToolIndex.index_mcp_tools` (line 407)
  indexes every MCP tool's description globally (keyed by generation, not by
  session), so a session's connector restriction is invisible to RAG
  retrieval — `get_tools_for_query` can and does nominate a tool name from a
  server this turn may not call. This is harmless: `_tool_schemas_for_route`
  (agent_loop.py, ~6978) only ever offers a schema that is BOTH in
  `route_relevant_tools` (RAG's opinion) AND in `route_mcp_schemas` (already
  connector-filtered) — a RAG hit with no matching schema is silently
  dropped, same as any other tool RAG nominates that got disabled for other
  reasons. F2 does not change `tool_index.py`.
* **Dispatcher** — `src/tool_execution.py::execute_tool_block` (line 1073,
  thin wrapper) → `_execute_tool_block_impl` (line 1334, does the real work).
  This is the ONE place every tool call passes through regardless of how it
  got there (native function-calling, fenced-block text parsing, a
  `_MCP_TOOL_MAP`-routed legacy name via `_call_mcp_tool` at line 741, or a
  bare `BUILTIN_EMAIL_TOOLS` name). F2.3's enforcement (see below) sits right
  after the existing `disabled_tools` / `tool_policy` blocks, before any
  admin/public-tool gate.
* **Scheduler / `task_policies`** — `src/task_scheduler.py`. `task_policies`
  is its OWN sqlite file (`_TASK_POLICY_DB`, a sibling to `core/database.py`'s
  main DB — see the module comment above `_policy_connect`, ~line 150), not a
  column on `ScheduledTask`. `set_task_policy`/`get_task_policy` (originally
  ~200/263, now a little further down after F2.2's addition) are the whole
  public surface. `TaskScheduler._execute_llm_task` (~2246) is where a
  scheduled task's `disabled_tools` gets composed today: the crew's
  `enabled_tools` allowlist (~2347) inverted into `disabled_tools`, the
  task's own declared `permissions` (AUTO-02) folded in as a further
  ceiling, then the global `disabled_tools` setting. This is a plain
  tool-*name* denylist — it has no concept of an MCP server id, which is
  exactly the gap F2.2/F2.3 fill for `mcp__<server>__<tool>` names.
  `_execute_llm_task` also creates (once) the task's own dedicated session —
  `task.session_id` — and every run after that reuses it; `_run_agent_loop`
  passes that same `session_id` into `stream_agent_loop` (→
  `_stream_agent_loop_body` → `_build_system_prompt` and `execute_tool_block`).
* **Projects** — `services/projects.py`. `ProjectStore` (class at line 268 in
  the pre-F2 file) persists to `data/projects.json`; `_validate` (identity
  fields only: name/folder/workspace/instructions) and `update()`'s
  `AGENT_OPTION_FIELDS` loop (the pattern for "additive per-project knob,
  absent key = untouched") are the two things F2.2 mirrors for the new
  `connectors` field (added next to the `AGENT_OPTION_FIELDS` loop, not
  inside `_validate` itself — see §Decisions). `project_for_session` /
  `project_context_for_session` (`_resolve_project_for_session`, ~1470)
  resolve a chat's project from `Session.project_id` first, the legacy
  folder match second — F2 reads through the same function, so a project
  bound either way is picked up identically.
* **`Session.behavior_mode` migration (the pattern F2.2 replicates for
  `Session.connector_ids`)** — `core/database.py`: the column itself is
  declared inline on the `Session` model (originally line 238, right after
  `crew_member_id`), a dedicated idempotent migration function
  `_migrate_add_session_behavior_mode` checks `PRAGMA table_info(sessions)`
  and does `ALTER TABLE sessions ADD COLUMN behavior_mode TEXT` only if the
  column is missing (guards a pre-existing `sessions` table; a fresh
  `create_all` already has the column and the ALTER is skipped), and the
  function is registered by name in `_formal_migration_steps()`'s list
  (`("add_session_behavior_mode", _migrate_add_session_behavior_mode)`,
  originally the last entry). Paired getter/setter
  (`get_session_behavior_mode`/`set_session_behavior_mode`) route through
  `get_db_session()` and are best-effort (log + return `None`/`False`, never
  raise). F2.2's `Session.connector_ids` / `_migrate_add_session_connector_ids`
  / `get_session_connector_ids`/`set_session_connector_ids` follow this
  exact shape.
* **Resumption / background** — `GET /api/chat/resume/{session_id}`
  (`routes/chat_routes.py`, ~4084) just re-subscribes
  (`agent_runs.subscribe`) to an already-running detached run keyed by the
  SAME `session_id` the run was started with; it does not rebuild the tool
  offer or re-enter the dispatcher with new context. The run itself (the
  `asyncio.Task` created by `POST /api/chat_stream` / `_run_detached`, not
  shown by `/resume`) keeps calling `_build_system_prompt` each round and
  `execute_tool_block` for each tool call, both already carrying
  `session_id` — see §Decisions for why this means resumption needs no
  separate wiring for F2. `src/crash_recovery.py::resume_plan` (line 434) is
  unrelated — it resumes a *changeset*/edit-cluster plan after a crash, not a
  chat or task's tool context, and is not part of this lot's scope.
* **`Session`/`McpServer`/`ScheduledTask` models** — `core/database.py`:
  `Session` (line 174), `McpServer` (line 1119, pre-existing — reused
  per-contract, never redefined here), `ScheduledTask` (line 1302,
  pre-existing, has `session_id` — the FK F2 relies on to find "which task
  is this session's own working session").

## Model (F2.2)

`connector_ids: list[str] | None` at three tiers, each an `McpServer.id`
list:

| Tier | Storage | Field |
|---|---|---|
| Session | `sessions.connector_ids` (new column, TEXT/JSON) | `core.database.get_session_connector_ids` / `set_session_connector_ids` |
| Project | `data/projects.json` row | `connectors` (validated in `services/projects.py`, see §Decisions) |
| Task | `task_policies.connector_ids` (new column, TEXT/JSON, sqlite sibling DB) | `src.task_scheduler.get_task_policy(...)["connector_ids"]` / `set_task_policy(..., connector_ids=...)` |

`None` at a tier = "this tier declared nothing" (today's behavior:
unrestricted). An explicit `[]` is a distinct, honoured value: "this tier
allows zero connectors" — never silently upgraded to "unrestricted".
Precedence: **task/session > project > unrestricted** (`src.connector_policy
.resolve_allowed_servers`; task wins over session when, unusually, both are
set — see that function's docstring).

## Enforcement (F2.3)

Single module: `src/connector_policy.py`.

* `resolve_allowed_servers(session=None, project=None, task=None) ->
  set[str] | None` — pure, no I/O. Exactly the contract's signature; each
  argument is that tier's OWN already-resolved value (a plain
  `list[str] | None`), not a session id or a lookup key.
* `is_tool_allowed(tool_name, allowed) -> bool` — pure. `allowed=None` always
  → `True`. Only gates names shaped `mcp__<server_id>__<tool>` (parsed with
  `str.split("__", 2)` so a tool segment that itself contains `__`, e.g. a
  browser action `mcp__<server>__browser_click`, keeps its suffix intact). A
  name that doesn't start with `mcp__`, or a malformed `mcp__`-prefixed name
  that has no server/tool segment, is let through (`True`) — see §Decisions
  for why built-in names are out of scope and why a malformed name is not
  this function's problem to reject.
* `resolve_allowed_servers_for_session(session_id, owner=None) -> set[str] |
  None` — the one impure helper every real call site uses. Looks up, in
  order: `Session.connector_ids` (session tier; short-circuits if not
  `None`); `ScheduledTask.session_id == session_id` → `get_task_policy(task.id)
  ["connector_ids"]` (task tier — this is how a scheduled task's declared
  allowlist is found from nothing but a `session_id`); `project_for_session
  (session_id, owner)["connectors"]` (project tier). Feeds the three into
  `resolve_allowed_servers`. Never raises — every sub-lookup is individually
  best-effort and degrades to "this tier contributed nothing".
* `tool_support_notice(session_id, endpoint_url, owner=None) -> str | None`
  (F2.4) — `None` unless `endpoint_url` is the text-only `faustus-cli://`
  transport AND this chat has an explicit connector selection somewhere in
  its chain. Advisory text only; never touches `endpoint_url`/`model`.

Wired at exactly two points, both already carrying `session_id`/`owner` — no
new parameter added to any function between them and the agent loop or the
scheduler (see §Decisions for why):

1. **Schema visibility** — `src/agent_loop.py::_build_system_prompt`, right
   where `mcp_schemas = mcp_mgr.get_all_openai_schemas(...)` is computed:
   filters the list with `is_tool_allowed` before it is ever returned to the
   caller (i.e. before the tool-RAG/token-budget selection downstream sees
   it). A resolution failure here fails OPEN (leaves schemas unfiltered,
   logs a warning) — this is advisory, defense-in-depth; the real boundary
   is #2.
2. **Execution** — `src/tool_execution.py::_execute_tool_block_impl`, right
   after the existing `disabled_tools`/`tool_policy` blocks and before the
   admin-tool gate: any `tool` starting with `mcp__` is checked against
   `resolve_allowed_servers_for_session(session_id, owner)`; a disallowed
   call returns `{"error": "Connector <server_id> is not enabled for this
   task", "exit_code": 1}` and logs a warning — never an unhandled
   exception, never executes. This is the actual enforcement boundary: it
   catches a call "by name" even when its schema was never offered (a model
   that remembers a tool name from an earlier turn, or guesses one).

Both points resolve **fresh from `session_id`** on every call — nothing is
snapshotted into a run. See §Decisions for why this is deliberate and what
it buys for resumption/background/scheduled runs "for free".

## Routes (F2.2 read/write surface)

* `GET /api/session/{sid}/connectors`, `PATCH /api/session/{sid}/connectors`
  (`routes/session_routes.py`) — body/response `{"connector_ids": [...] |
  null}`; GET also returns `{"effective": [...] | null, "source": "session" |
  "project" | "all"}`. **Deviation from the contract's literal path**: the
  contract says `PATCH /api/sessions/{id}`; this file's own convention is
  singular `/session/{sid}` for every other single-resource verb (`PATCH
  /session/{sid}` already exists for rename/model-switch, `GET /session/{sid}
  /draft`, etc. — `/sessions` plural is reserved for collection operations
  like `GET /sessions` and `POST /sessions/bulk-delete`). Added as new
  sub-routes rather than adding a `connector_ids` form field to the existing
  `PATCH /session/{sid}` (which is `Form(...)`-based, not JSON, and a list
  field does not fit that shape) — additive, zero risk to the existing
  contract (principle 10).
* `GET /api/session/{sid}/tool-support` (F2.4) — `{"supported": true |
  false | "unknown", "mode": "api" | "ollama_native" |
  "ollama_openai_compat" | "fenced" | "text_only" | null, "reason": str}`.
  Read-only; never probes/loads a model. `"unknown"` when the session has no
  model/endpoint configured yet, or the lookup itself fails.
* `GET /api/projects/{id}`, `PATCH /api/projects/{id}`
  (`routes/project_routes.py`) — `ProjectUpdateRequest.connectors:
  Optional[List[str]]`; both responses are annotated with
  `connector_ids`/`effective`/`source` (`source` is only ever `"project"` or
  `"all"` — a project has no tier above it besides unrestricted). **Known
  gap**: `update_project` reads the body with `payload.model_dump
  (exclude_none=True)`, the same as every other field in that model — so a
  PATCH can set `connectors` to a concrete list or to `[]`, but cannot send
  an explicit `null` to clear a previously-set list back to "no
  project-level restriction" (indistinguishable from "the caller didn't
  mention it"). This is a pre-existing limitation of that endpoint's body
  handling (shared by every other nullable-but-not-boolean field there,
  e.g. `test_command`/`review_model` use `""` as their own "clear" value)
  and was not changed for one new field — changing `exclude_none` globally
  for that model was out of scope and riskier than living with the gap.
* `POST /api/tasks`, `PUT /api/tasks/{task_id}` (`routes/task/task_routes.py`)
  — `TaskCreate.connector_ids`/`TaskUpdate.connector_ids: Optional[list[str]]`,
  written through `set_task_policy(..., connector_ids=...)` next to the
  existing AUTO-02 fields. `GET /api/tasks`, `GET /api/tasks/{id}` responses
  gain a `connectors` object: `{"connector_ids": [...] | null, "effective":
  [...] | null, "source": "task" | "session" | "project" | "all"}` (`_task_
  to_dict` / `_task_connectors_payload`). Same known gap as projects: PUT
  cannot explicitly clear a declared `connector_ids` back to "undeclared"
  through this endpoint (only to a concrete list, `[]` included) —
  `set_task_policy`'s new `_clear_connector_ids=True` kwarg exists for that
  case but is not wired to any route yet (documented, not exposed, to avoid
  inventing a request shape the contract didn't ask for).

## Decisions

* **Resolve fresh from `session_id`, don't thread a new parameter.** The
  naive reading of F2.3 ("cubrir chat normal, tarea en background,
  reanudación, tarea programada... el scheduler pasa `connector_ids` de la
  tarea al contexto de ejecución") suggests adding an `allowed_connectors`
  parameter to `execute_tool_block`, `_execute_tool_block_impl`,
  `stream_agent_loop`, `_stream_agent_loop_body`, `_run_agent_loop`, and
  every closure in between, then having the scheduler and the chat routes
  each compute it once and pass it down. That is a lot of signature surface
  to touch in files this large and this heavily cross-referenced, and every
  one of those call sites already carries `session_id` end-to-end
  (including the scheduler: `task.session_id` IS the session the agent loop
  runs against — see `_execute_llm_task`). So instead, both enforcement
  points call `resolve_allowed_servers_for_session(session_id, owner)`
  themselves, resolving fresh from persisted state (DB + `task_policies.db`
  + `projects.json`) on every use. This has three effects, all intentional:
  1. **Zero new parameters** on any function between the two enforcement
     points and their existing callers.
  2. **Resumption and background runs are covered automatically** — `GET
     /api/chat/resume/{id}` reconnects to a run that is still calling
     `execute_tool_block`/`_build_system_prompt` with the same
     `session_id` it always had; there is no separate "resumed" code path
     to wire.
  3. **A live edit takes effect on the very next tool call**, not just the
     next turn — changing a session's `connector_ids` mid-run changes what
     the very next `mcp__` call is allowed to do. This was not explicitly
     asked for but falls out of "resolve fresh" for free and is a strict
     improvement over a value snapshotted at turn start.
  The cost is a few extra DB/file reads per tool call (one sessions-table
  query, one best-effort `ScheduledTask` lookup, one `projects.json` read
  through the existing in-memory `ProjectStore` cache) — acceptable given
  today's tool-call volumes, and every one of those reads was already
  individually best-effort infrastructure before this lot.
* **Minimum scope is `mcp__<server_id>__<tool>` names, not built-in tool
  names.** F2.5's own task description hedges this ("y también las built-in
  de correo si el catálogo las trata como conector — decide y documenta:
  como mínimo MCP"). Decision: **out of scope**. `is_tool_allowed` only
  gates names that parse as `mcp__<server>__<tool>`; a bare built-in name
  (`send_email`, `list_emails`, `read_file`, `bash`, ...) — including the
  `BUILTIN_EMAIL_TOOLS` bare-name aliases that `_execute_tool_block_impl`
  internally re-routes to `mcp__email__<tool>` — is untouched. Rationale:
  those are Faustus's own built-in capabilities (not a user-added Hoard-style
  connector from the F1 catalog), already gated by their own mechanisms
  (`_ADMIN_TOOLS`, `is_public_blocked_tool`, the existing `disabled_tools`/
  `ToolPolicy` machinery traced above), and folding them into connector
  policy would silently change what "select a task's connectors" means for
  every existing task that has never heard of this lot. The one test this
  decision affects (F2.5: "tarea con `connector_ids=[]` no puede llamar mail
  ... aunque exista") is satisfied for the qualified `mcp__mail__...` (or, in
  the test suite, `mcp__<mail-server-id>__...`) spelling, which is the
  literal minimum the contract asks for; see
  `test_scheduled_task_empty_connector_ids_blocks_mail_even_though_it_exists`
  in `tests/test_connector_policy.py`.
* **`connectors` validated in `ProjectStore.update()`, not literally inside
  `ProjectStore._validate`.** `_validate` (identity fields: name/folder/
  workspace/instructions) is called from both `create()` and `update()` and
  raises `ProjectError` on a bad value — the wrong shape for "drop silently
  invalid list entries, keep the rest, log a warning, never raise" (the
  contract's own rule for this field). `connectors` is handled the same way
  every other additive per-project knob already is (the `AGENT_OPTION_FIELDS`
  loop right next to it in `update()`): absent key = untouched, present key
  = validated and written (`services.projects._sanitize_connector_ids`,
  which queries `McpServer.id` and drops anything not found, keeping order
  and de-duplicating). Functionally this is still "validated in
  `services/projects.py`, in `ProjectStore`" — just not inside the one
  method literally named `_validate`, which was never given a `connectors`
  parameter to avoid touching its signature/behavior for `create()` too
  (creation-time `connectors` was not required by the contract).
* **`_agent_route_tool_mode`'s boolean triad does not itself carry
  "unsupported".** `is_api_model`/`is_native_ollama`/`ollama_openai_compat`
  only decide HOW tools are offered (native function-calling schemas vs.
  Faustus's own fenced-block text instructions) — a model that gets `False`
  for all three still gets tools via the fenced mechanism and can use them
  normally. The ONE case with no tool support at all is the text-only CLI
  transport (`endpoint_url` starting `faustus-cli://`), which
  `_agent_route_tool_mode` already special-cases with an early return, and
  which the agent loop already tracks separately as `text_only_transport`
  (`_build_route_request_state`, `_tool_schemas_for_route` strips MCP
  schemas to `[]` for it). `GET /api/session/{sid}/tool-support` and
  `tool_support_notice` both key off exactly this, not off the api/native/
  compat triad — `"supported": false` is `faustus-cli://` only,
  `"supported": true` covers both native and fenced tool-calling models.
* **No fallback, ever.** Neither `tool_support_notice` nor the `/tool-support`
  route reads or writes `Session.model`/`Session.endpoint_url`; both are
  pure reads. `stamp_connector...` (via `save_assistant_response` in
  `routes/chat_helpers.py`) only ever adds `metadata["notice"]` to the saved
  assistant message — it cannot change what actually ran the turn. The
  wording of the stamped notice was deliberately checked (see
  `test_tool_support_notice_never_mentions_switching_model_or_endpoint`) to
  never imply a substitution happened.
* **Fail-open on an internal connector-policy error, not fail-closed.**
  Both enforcement points wrap their `resolve_allowed_servers_for_session`
  call in a `try/except` that logs and falls through to "not blocked by
  connector policy" on failure — consistent with every other best-effort
  lookup already in this codebase (`_load_mcp_disabled_map`,
  `get_session_behavior_mode`, `project_for_session`, ...), all of which
  degrade to "this feature contributed nothing" rather than breaking the
  turn. A connector restriction is additive user intent, not a security
  boundary imposed on the user by someone else — degrading it to
  "unrestricted" (today's default for every existing session/task) on an
  unexpected internal error is the same posture as every other optional
  policy layer here, not a new risk.

## Not this lot

* `src/connectors.py`, `src/connector_sidecar.py`, `routes/connector_routes.py`
  (F1's catalog/presets/sidecar/health) — not created here, not imported by
  anything in `src/connector_policy.py`. F2's policy is defined purely in
  terms of `McpServer.id`, which existed before F1 or F2.
* The Studio `/connectors` screen, `ConnectorPicker`, and the adapters that
  will call the routes documented above — F3.
