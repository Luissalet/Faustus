# Project coordination with Agora

In **Project → The agent here**, select an existing Hub connector and the verified project folders in which Faustus may coordinate work. Save once. New conversations in that project inherit the configuration; the normal Studio composer shows whether coordination is available. No account, actor ID, tool name or read cursor needs to be entered by the user.

The connector must already be connected. Folder choices are checked against the Hub repository catalogue and local Git lineage, including isolated clones. An unavailable catalogue or unverified folder is reported rather than guessed. Saving an active configuration does not start applications or model servers.

## Scope and identity

Each chat receives a stable, product-generated `faustus-…` actor. Chats have separate bindings, read cursors, pending details, task observations and delivery receipts. The project template records the connector, allowed operations, repositories, resources and coordination threads. Updating it invalidates in-flight grants; the next turn refreshes the binding. Revocation is available even when the connector is offline. An explicitly revoked chat is not silently re-enabled by its project template.

Authorized Hub reads are global: the agent can inspect other tasks and conversations for context. Repositories, path resources and thread selections restrict **writes and publication**, not Hub read visibility. A task write requires a native observation of that task and matching scope; progress, submission and closure additionally require ownership. Locks cannot take over another actor's task. Heartbeats renew only the current chat actor's scoped locks. The common coordination thread is included by the project UI.

For a reply to a task discussion, the product can infer the task from the exact thread in a delivered native task observation. The model does not need to repeat a helper task ID. An unknown thread, invalid argument, revoked binding or out-of-scope action returns an actionable error to the model; it does not open a broad approval card that could not fix the failed native authority check. These checks also apply when general tool approval is disabled.

Coordination grants do not grant shell execution, arbitrary file writes, model loading, settings changes or administration. Those operations retain the existing product permission checks. A successful coordinated turn does not prove general coding autonomy or model parity.

## Native tools

The agent loop offers fourteen typed tools when the saved binding is valid:

| Read tools | Work and publication tools |
| --- | --- |
| `coordination_poll`, `coordination_detail`, `coordination_task`, `coordination_board` | `coordination_heartbeat`, `coordination_add`, `coordination_claim`, `coordination_checkpoint`, `coordination_post`, `coordination_submit`, `coordination_review`, `coordination_done`, `coordination_lock`, `coordination_unlock` |

Identity and connector are supplied by the runtime, never tool arguments. A read-only turn exposes the four read operations; it cannot perform housekeeping or writes. Existing connector allowlists, disabled tools, owner boundaries and per-run tool policy still apply.

`poll` saves a bounded inbox page and returns compact task status and pending message references. An excerpt is not a complete read. `detail` returns up to 4,000 characters and `next_offset`; follow the chunks until the whole message has been delivered.

Native receipts are confirmed only after the full tool output survives formatting, offload, trimming and transport conversion, appears in the final request to the model and that candidate responds. Tool execution, UI events, shortened previews, dropped requests, HTTP errors and cancellation do not confirm delivery. The stored snapshot and its digest prevent a changed body from inheriting an old receipt. This mechanism does **not** send an inbox ACK to the Hub.

Before reviewing, read the task and its complete discussion details, test the submitted commits and vote with the submission revision that was observed. Checkpoint revisions are separate from submission revisions. A stale vote is rejected; it must not be repeated with an automatically refreshed revision. The current task-discussion snapshot is limited to 500 messages and fails explicitly above that limit.

`checkpoint` accepts a required `summary`, optional workspace, branch, full base/head hashes, next steps, artifacts and tests shaped as `{name, status, evidence}`. Test status is `passed`, `failed` or `not_run`; declared evidence is not independently certified. `submit` accepts the summary and full commit hashes without requiring opaque optional blocks.

An interrupted write or an unreadable write response is reported as **outcome unknown**, non-retryable. Inspect the actual Hub state before deciding whether another action is needed. A successful write followed by revocation is not reported as a harmless permission denial.

## API and MCP

The normal REST-to-MCP bridge discovers these routes from OpenAPI:

| Route | Purpose |
| --- | --- |
| `GET/PUT /api/coordination/projects/{project_id}` | Inspect or save the owner-managed project template; updates use `expected_revision`. |
| `GET /api/coordination/projects/{project_id}/choices?connector_id=…` | Read verified folder/repository choices through the configured Hub. |
| `GET/PUT /api/coordination/{session_id}` | Inspect availability or manage an explicit chat binding. GET does not materialize a new binding. |
| `POST /api/coordination/{session_id}/actions` | Run one typed operation, subject to the binding and optional strict `read_only`. |
| `POST /api/coordination/{session_id}/delivery` | Explicitly confirm one detail or task receipt after an API client has consumed it, with the returned binding revision. |

API execution does not itself confirm receipt: a disconnected response leaves the detail pending. Receipts cannot be used in another chat. Owner checks use the existing installation mode, including local no-login mode, without introducing accounts.

Storage is a local SQLite database in the application data directory. The connector configuration is fingerprinted; changed or disabled configuration requires an explicit refresh. Credentials themselves are not stored in the coordination record. Existing conversations and user files are not migrated or reset.

## Verification boundaries

Focused tests cover scope, owner separation, revisions, unknown write outcomes, durable cursors, revocation, full discussion review and final-request delivery. Isolated API and real stdio MCP checks cover project configuration, separate chat actors, cursor persistence, denied writes and fragmented receipts. Browser checks exercise the normal Project and Studio controls. Model-backed usability must be measured separately with the configured model; simulated-loop checks do not establish that the agent works comfortably without manual guidance.
