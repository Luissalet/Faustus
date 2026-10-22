# Conversation objective

Studio can keep one editable brief for a saved conversation, including chats
without a project. The objective has a stable identity, revision, statement,
context/sources and expected deliverable. Defining it creates no project,
automation or Hoard process. Incognito and group-room interfaces retain their
existing behaviour.

The objective bar opens the brief editor. Link an existing project task, child
conversation, automation or file by its native identity. A change reference
requires both its repository and file, with **Staged** or **Unstaged** selected
explicitly. It opens the existing diff renderer; file links open the normal
workspace viewer. Agent and task buttons reuse the existing side panels.
References are bookmarks, not claims that an artifact or its quality has been
verified. Existing artifact and harness verification remains authoritative.

Each detached chat run started while the objective is active is associated with its
exact run ID and the brief revision at its start. Earlier brief versions stay
available with their run associations. A paused or completed objective does not
claim later unrelated turns. Linked activity uses the existing turn-activity
view for that exact run; missing detailed history is reported without silently
substituting another run.
The saved brief enters both ordinary HTTP chat and streaming chat as user
context before the latest message, without changing the person's saved message.

**Pause objective** requests the current generation's next safe point, preserving
the operation already in progress. The objective marker, generation state and
automation state are different. Linked automations keep their own schedule and
status. The displayed time is wall time with the objective active, including
time between turns and process restarts; it is not model execution time.

**Continue objective** rejoins an active run, or starts a deliberate new
continuation from the saved history and brief using the model selected in the
composer. It never replays tool effects. A durable receipt preserves the accepted
run identity after the ordinary send outbox expires. Repeating that same
expected revision returns its receipt. If dispatch outcome cannot be established,
the response says so and does not resend it. The person can inspect history and
issue a new continuation. SSE reconnection can name `run_id` explicitly and will
not subscribe to a replacement run.

**Complete objective** records an explicit delivered-result statement. Finishing
a stream, percentage of changed files or an empty result cannot complete it.
Completion does not stop a generation or an automation. Editing uses SQLite CAS
across processes: stale writes return409. Studio retains the draft, shows the
saved version and lets the person choose a revision before saving again.
Deleting the parent conversation cleans its brief, versions and associations;
native linked work and files are retained.

## HTTP and MCP

- `GET /api/session/{session_id}/objective`: brief, native run identities/status
  and current generation, without starting any work.
- `PUT .../objective`: `{expected_revision, brief: {statement, context,
  deliverable, refs}}`. Use revision0 only when no objective exists.
- `POST .../objective/state`: `{expected_revision, status: active|paused|complete,
  result?}`. Activating a marker alone starts no generation.
- `POST .../objective/continue`: `{expected_revision, selected_model?,
  selected_endpoint_id?, selected_endpoint_url?, workspace?}`. Reuses the native
  chat route, ownership, route selection, execution defaults and outbox.
- `GET /api/chat/resume/{session_id}?run_id={id}&cursor={n}`: normal SSE
  subscription, optionally restricted to the exact runtime identity.

`mcp_servers/conversation_objective_server.py` exposes the same owner-aware API
as `conversation_objective`, actions `get|save|state|continue`. Reads start no app.
Use the current revision returned by `get`; never turn a stale write into a new
write automatically. The existing `FAUSTUS_INTERNAL_API_URL`/`FAUSTUS_URL` and
token configuration apply. No hosted API or new model provider is required.

Reference shapes:

```json
[
  {"kind":"task","project_id":"existing-project","id":"existing-task"},
  {"kind":"agent","id":"existing-child-conversation"},
  {"kind":"schedule","id":"existing-automation"},
  {"kind":"resource","workspace":"C:/work/game","path":"docs/report.md"},
  {"kind":"change","id":"existing-repository","workspace":"C:/work/game",
   "path":"src/player.cs","scope":"unstaged"}
]
```

Persistence is `DATA_DIR/conversation_objectives.sqlite3`, following the native
SQLite store pattern. No separate execution scheduler or agent platform is
introduced. Goal metadata does not replace the existing typed Creator goals,
project tasks, run journal or artifact stores.
