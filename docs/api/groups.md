# Persistent working groups

Group chat keeps its composition and ordered turns in the server. The parent and participant conversations remain ordinary canonical Studio sessions. Each participant runs through the normal chat endpoint, native agent run, configured model credentials, tool selection and question handling. There is no additional agent loop or operational dependency on another application.

New groups use the configured global model, currently the resident Sparks service, and agent mode. Models and personas can be selected per participant. Plain chat, parallel discussion and ordered turns remain available. There is no fixed participant ceiling. A working folder is optional; native project and tool behavior still applies.

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
