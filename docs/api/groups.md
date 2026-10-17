# Persistent working groups

Group chat keeps its composition and ordered turns in the server. The parent and participant conversations remain ordinary canonical Studio sessions. Each participant runs through the normal chat endpoint, native agent run, configured model credentials, tool selection and question handling. There is no additional agent loop or operational dependency on another application.

Studio defaults to a collapsible **Debate** with live participant activity and one separate final answer. Create/update/send and the corresponding MCP tools accept `debate: true`; native clients that omit it retain their existing format. The room editor keeps **Group chat** available for separate replies. During a debate, members inspect evidence, resolve ordinary choices and nominate one reporter with `[REPORTER: @handle]`. When they converge or the configured reply rounds finish, the conductor queues exactly one `final:<origin_turn_id>` turn for that reporter; the deterministic request ID prevents duplicate final answers after recovery or acknowledgement retries. Without a nomination, the last member with a useful successful reply reports back. The final turn does not start another peer discussion.

The collapsed block retains all replies, reasoning, tools and errors; it shows who is active, when a decision is needed, and when the reporter is preparing the final answer. Actual native questions remain outside the collapsed block. Stop preserves queued user instructions and their stopped status. Final replies and their provenance are canonical history records (`group_debate`, `group_origin_turn_id`, `group_final`); internal peer/final prompts are never shown as messages written by the person. A stopped debate is not silently resumed or finalized.

New groups use the configured global model, currently the resident Sparks service, and agent mode. Models and personas can be selected per participant. Plain chat, parallel discussion and ordered turns remain available. There is no fixed participant ceiling. A working folder is optional; native project and tool behavior still applies.

Project → Agora opens the same canonical group in Studio itself, including its
regular conversation pane, header, transcript and composer. The project chip
returns to details; Tasks opens the connected task ledger. Members
accept `reasoningEffort` and `thinkingEnabled` overrides in create/update and
presets; omitted values inherit and explicit `false` is preserved. Supported
choices come from the selected endpoint/model. Groups MCP has the same fields.
`discussionRounds` defaults to `-1` (continue until discussion ends), `0` disables
peer follow-ups, and positive values select an explicit round limit. Existing
saved limits are preserved. `PATCH /api/groups/{id}` accepts `projectId` to link
an older unbound room and its member sessions without replacing their history.
Observed participant activity is persisted in `step.detail.activity`, including
start, last event, finish, phase, tool and event count. Polls do not renew the
last-event timestamp. A silent or disconnected run is not labelled successful.

Create/update/send accept `executionOptions`, the regular composer's native chat
fields (attachments, generation, tools, context, memory, skills, behavior and
execution preferences). Each queued turn snapshots them. Participant identity,
route and request IDs remain owned by the conductor; explicit member reasoning
and autonomy override shared defaults. Attachments stay in the canonical user
message. A send carries options, execution mode and workspace in one request,
without a browser PATCH racing with streaming progress. The groups MCP schemas
expose the same options.

The browser and MCP are observers. Disconnecting or opening another page does not stop work or prevent the next participant. Messages join the FIFO queue. A repeated `request_id` returns the same turn. Explicit Stop cancels only this group's active native run IDs and queued steps. Closing a group preserves its Studio transcript.

Completed replies and actual native tool outputs (`tool`, `command`, `output`, `exit_code`, `call_id`) are available to later participants. User clarifications are shared too. Parallel participants see previous rounds; ordered participants also see the preceding participant's completed work. Questions retain their native identifiers and revisions and resume the same participant.

| Method | Path | Result |
|---|---|---|
| GET | `/api/groups/config` | Configured global route and supported modes, without credentials |
| GET / POST | `/api/groups` | List groups / create canonical sessions and persist composition |
| POST | `/api/groups/adopt` | Adopt an existing browser group without replacing its sessions |
| GET | `/api/groups/{id}` | Composition, FIFO turns, partial/completed steps, run IDs and errors |
| POST | `/api/groups/{id}/messages` | Queue `message`, stable `request_id`, optional participant `mentions` |
| POST | `/api/groups/{id}/resume` | Resume with `message` and existing question/approval answer fields |
| POST | `/api/groups/{id}/stop` | Explicitly cancel only this group |
| POST | `/api/groups/{id}/archive` | Stop and close, keeping canonical conversations |

The built-in `groups` MCP server offers `group_options`, `group_list`, `group_create`, `group_get`, `group_send`, `group_resume`, `group_stop` and `group_archive`. It calls these native routes. `FAUSTUS_INTERNAL_API_URL` selects its Faustus instance and `FAUSTUS_INTERNAL_TOOL_TOKEN` carries the existing internal identity. With authentication enabled, the sole session owner is inferred; set `FAUSTUS_GROUPS_OWNER` explicitly for a server with several owners. Ordinary browser ownership remains authoritative, and existing API-token scopes and human approval rules remain in force.

Existing browser groups migrate on a genuine missing-group response only, retaining their parent, children, chosen models and plain-chat behavior. The browser cache is just Studio's navigation index; the server is authoritative. Presets remain in the existing preset API.

After a restart, completed steps are projected idempotently and finished native outbox results are recovered. A live matching native run can be observed again. A interrupted attempt with unknown effects is surfaced and is not blindly repeated. No automatic claim of successful completion is made for that attempt. Expired driver leases are recovered without needing another browser request. Physical model context and explicitly configured harness controls still apply.

Verification covers FIFO/deduplication, two coordinators, observer departure, Stop isolation, restart recovery, ownership, exact request/run IDs, replacement of retracted text, pending questions, shared actual tool results and native API/MCP. A private full product instance also ran two configured scripted models, four real file reads after MCP departure, and a browser that left during generation. The resident global GLM completed two readers and verified a private file after MCP departure. These tests establish the covered behavior; they are not a universal external-harness ranking.
