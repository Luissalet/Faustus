# Shared project tasks

Projects → Tasks is the single project board used by the person and agents.
Objectives, descriptions and acceptance criteria belong to each task. Clicking
the entire card opens a wide modal with title, priority, assignee, text,
acceptance, clarifications and the existing task conversation. The board fills
the available workspace width. Search and assignment filters use the same tasks.
Status, type and free labels are editable in the same modal. Kanban supports
dragging between normal work states; a table, filters, Markdown export and the
existing file-import preview also operate on these shared records. Pending
reviews use the submission workflow and cannot be bypassed by marking done.

Projects → Chats includes its saved Agora rooms and their participants. Opening
a room returns to the existing Studio interface, conversation selector, Members,
native composer and collapsible Debate. No histories or room IDs are replaced.

| Method | Project-relative path | Operation |
| --- | --- | --- |
| POST | `/tasks/prepare` | Bind the project and import previous local records once |
| GET | `/tasks` | Read all pages of its canonical shared tasks |
| POST | `/tasks` | Create title, body, acceptance, priority (0–3), owner and note |
| GET | `/tasks/{id}` | Read full description, history and current `etag` |
| PATCH | `/tasks/{id}` | Edit fields or add a note using `expected_etag` |

Paths start with `/api/projects/{project_id}` and enforce the existing project
owner/admin access. Supplied agent or space overrides are not accepted. Existing
agent workflows still claim resources, submit deliveries and review revisions;
editing acceptance during review invalidates that review. Concurrent edits fail
with HTTP409 and retain the browser draft. Assignment transfers the task's leases;
clearing an assignment releases them rather than leaving an ownerless active lock.

`project_task_list`, `project_task_get`, `project_task_create` and
`project_task_update` in the existing project workspace MCP server call these
same routes. Read before editing and pass the returned etag. Create accepts an
optional stable `request_id`. The existing shared repository also exposes
`hub_agora_task_edit` for compatible agent clients; this is storage/transport
compatibility, not a second user board or a dependency on the Hub's prototype UI.

Preparation imports prior objectives, requirements and issues using a stable
project/source ID. Reopening the board cannot overwrite a task subsequently
edited by a person or agent. Original source records remain intact; imported
snapshots retain older IDs, relationships, requirement revisions, links and issue
history, including the full available objective audit/evidence log. Rejected and
superseded requirements remain terminal; objective priorities 1–4 map to 0–3.
They can be inspected under Previous history and evidence in the task modal.
Titles accept 300 characters; descriptions, acceptance and clarifications accept 200,000.
Larger records are refused explicitly rather than truncated, with an import
error and retry alongside the still usable canonical board. Older source
APIs remain compatible; new project task operations use the shared ledger.

After preparation the project durably adopts this ledger. The normal `board_*`,
`project_objectives`, `req_*` and meeting action writers then project the same
records, including comments, statuses, dependencies and evidence. Original issue,
objective and requirement IDs remain usable aliases; new compatibility IDs are
`T:<canonical_id>`. Legacy source databases/files are retained as migration
sources, without mirroring later edits back into another task authority. This
also keeps the agent's ordinary board/objective context aligned with user edits.
Projects not yet adopted retain their older API behavior until preparation.

Task detail includes `edits`: ordered revisions with author, timestamp, and
before/after values for title, body, acceptance, priority and owner. Creation
records the initial criteria; existing tasks receive a labelled baseline when
upgraded. The modal exposes both values for each changed field. Clarifications
remain append-only messages in the same conversation.

No model is invoked by editing the board or navigating rooms. This change does
not repair historic failed model replies or silently resume stopped discussions.
