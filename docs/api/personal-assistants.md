# Personal assistants

Home → **Your assistants** creates a named continuing conversation or adopts an
existing chat. Open its name to enter ordinary Studio, including mobile/PWA.
The assistant ID is the canonical session ID. There is no second transcript,
agent executor, model configuration or project memory.

Names and descriptive usage notes persist in `personal_assistants.sqlite3` in the
configured data directory, scoped to the current owner. Working instructions
remain those of the project and conversation. Notes do not silently become a
system prompt. The list shows the last canonical user/assistant message and the
actual active-run state; closing the page does not cancel a server-side turn.
Voice, tools, files and notifications use the existing chat services.

| API | Behaviour |
| --- | --- |
| `GET /api/mobile/assistants` | List active assistant references with session, project, preview and observed state. |
| `POST /api/mobile/assistants` | Save `{name, description?, session_id?, project_id?}`. Adopt an owned session, or create one through the ordinary session API using the configured model and project defaults. Creation starts no generation. |
| `PATCH /api/mobile/assistants/{session_id}` | Update name/description; project remains the canonical chat's project. |
| `DELETE /api/mobile/assistants/{session_id}` | Archive the reference, preserving chat and messages. Adopt that chat again to restore it. |

The native builtin MCP server **personal_assistants** exposes one tool with
actions `list`, `create`, `update`, `remove` and the same fields. Use ordinary
chat or `voice_session` with the returned `session_id` to work with it.

A deleted chat appears unavailable and is never silently replaced. An archived
chat is marked archived. Existing companion authentication and remote access
apply; this feature does not provide a new public relay, native iOS client,
messaging-channel bridge or separate autonomous scheduler.

Validation uses isolated canonical chat databases, owner/project isolation,
remove/restore preservation, real HTTP and MCP transport and desktop/mobile
browser creation and reopening. It does not establish speech-recognition
accuracy or remote-network delivery performance.
