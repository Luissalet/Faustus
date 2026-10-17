# Personal assistants

Studio → new conversation → **Your assistants** creates an assistant or adopts
an existing chat. Open its name to enter the conversation, including mobile/PWA.
The assistant ID is the canonical session ID. There is no second transcript,
agent executor, model configuration or project memory.

Names, descriptive notes, **working instructions** and explicitly selected
published **skills** persist in owner-scoped `personal_assistants.sqlite3`.
Instructions are separate from notes and applied on every normal or scheduled
turn, outside the shared base-prompt cache. Selected procedure bodies supplement
automatic skill selection, including lean chat and short follow-ups. Explicit
`no_skills` and the owner's skills preference still apply. Unavailable pins are
reported; active project skills retain normal project activation/disclosure.

**Proactivity** uses one stable native `ScheduledTask`, the configured global
model and the ordinary harness. Choose hourly, daily, weekly or five-field cron,
IANA timezone, mission, completion notifications and silence without meaningful
updates. Hourly uses native cron at the selected minute. Creating an assistant
does not enable autonomous work unless explicitly configured. Run an enabled
mission now from its row, or disable proactivity to pause future runs and cancel
its active native task. Remove also pauses work; restoring without a new explicit
schedule keeps it paused, preserving instructions and selected skills.

Scheduled turns read canonical chat history and current configuration, preserve
project context, and append results to that same chat with proactive source
metadata. Failed tool runs are not replayed as text-only completions. A no-update
sentinel, including repetition from an automatic continuation, becomes a native
skipped run before chat delivery or notifications. Five recent native runs,
failures, last run and next run are visible in the editor/API. Completion
notifications can be disabled; errors remain visible. Existing leases,
occurrence keys, queue, foreground priority and timezone/DST handling remain
authoritative. Faustus must be running; it cannot wake a powered-off computer.
Voice, files and ordinary turns continue using existing chat services.

| API | Behaviour |
| --- | --- |
| `GET /api/mobile/assistants` | Profiles, canonical session/project, preview/state, proactivity and five recent native runs. |
| `GET /api/mobile/assistants/skills` | Owner's published skill names/descriptions, including explicitly global skills. |
| `POST /api/mobile/assistants` | `{name, description?, session_id?, project_id?, instructions?, skills?, proactivity?}`. Adopt an owned chat or create through the ordinary session API. Creation starts no generation. |
| `PATCH /api/mobile/assistants/{session_id}` | Partial update; omitted profile/proactivity fields survive. Project remains the canonical chat's project. |
| `POST /api/mobile/assistants/{session_id}/run` | Execute an enabled mission via native scheduler; `started:false` if already queued/running. Paused missions return 409. |
| `DELETE /api/mobile/assistants/{session_id}` | Pause native work and archive the profile, retaining chat/messages/configuration. Adopt again to restore. |

The native builtin MCP server **personal_assistants** exposes one tool with
actions `list`, `skills`, `create`, `update`, `run`, `remove` and the same fields.
`proactivity` fields: `enabled`, `schedule`, `time` (`HH:MM`), `day` (0=Monday),
`timezone`, `cron`, `prompt`, `notifications`, `quiet`. Invalid schedules are
rejected before profile mutation. Use ordinary chat or `voice_session` with the
returned `session_id` to work interactively.

A deleted chat appears unavailable and is never silently replaced. An archived
chat is marked archived. Existing companion authentication and remote access
apply; this feature does not provide a new public relay, native iOS client,
messaging-channel bridge or separate autonomous scheduler. It does not claim a
general autonomous event planner or full reference-project parity. Delegated
coworker profiles and singleton assistant check-ins remain available, without
migration. Memory/Brain grouping is separately tracked in Ágora #723.

Validation uses isolated canonical chat databases, owner/project isolation,
remove/restore preservation, real HTTP and MCP transport and desktop/mobile
browser creation and reopening. It does not establish speech-recognition
accuracy or remote-network delivery performance.
