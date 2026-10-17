# Dictation workspace

Global desktop dictation now uses the same faithful editor as Studio. The raw
transcript and final text are saved before attempting desktop delivery. A failed
paste returns `pasted:false`, `recoverable:true` and a history `id`, instead of
discarding the text. No automatic retry types the text a second time.

Voice settings contains vocabulary and history: copy, correct, inspect the
original and delete. Correcting a spelling can learn nearby name substitutions;
numbers, paths, negations and sentence rewrites do not become learned entries.
The current editor still validates quantities and faithful deletion before
applying explicitly saved vocabulary. Quoted text, URLs, paths and code are kept
literal. Vocabulary applies once, so replacements cannot cascade.

The general vocabulary applies to desktop dictation and Studio. Project entries
override it for Studio voice in that project, or API clients supplying `project`.
Spoken revision retains its existing draft/undo behavior. Existing Whisper,
Parakeet, endpoint and command providers are preserved. The model used for
editorial cleanup is the configured global service; an unavailable editor keeps
the recognized text. No new text model or cloud speech service is introduced.

## API

All workspace records are owner-scoped in `dictation.sqlite3` under the normal
data directory. This stores text, creation time, target title and delivery state,
not audio. Desktop input routes keep their existing host-local restriction.

| Method | Route | Payload or result |
|---|---|---|
| GET | `/api/dictation/vocabulary?project=` | `entries` includes general and selected project entries |
| POST | `/api/dictation/vocabulary` | `{heard,replacement,project?}` upserts a spelling |
| DELETE | `/api/dictation/vocabulary/{id}` | Deletes that owner's entry |
| GET | `/api/dictation/history?project=&before=&limit=30` | Recent text records in that scope |
| PATCH | `/api/dictation/history/{id}` | `{text,learn:true}` corrects text, returns learned spellings |
| DELETE | `/api/dictation/history/{id}` | Deletes a text record, not its learned vocabulary |
| POST | `/api/stt/polish` | Existing editor, with optional `project`; clean dictations return history `id` |
| POST | `/api/dictation/transcribe-and-paste` | Existing multipart route adds optional `project` and history `id` |

The built-in `voice` MCP server adds `dictation_workspace`, with actions
`vocabulary`, `put`, `delete_vocabulary`, `history`, `correct`, `delete_history`.
Workspace API tokens use the existing `sessions` scope. These actions never type
into an application. Copying recovered text is an explicit user action.

## Verification and limits

Area tests cover ownership, project overrides, persistence, non-cascading spelling,
literal text and quantities, name learning, deletion, failed paste recovery and
storage before delivery. Desktop tests preserve the recorder on its initial
`recording` event and ignore repeated hotkeys during processing. The hotkey is
still press once/start and press again/stop, not a held-key implementation.

Native API/MCP/browser results and any real speech/model measurements are recorded
separately in the task evidence. Unit fixtures do not establish microphone
accuracy, Spanish recognition quality, Windows foreground delivery or model speed.
The spelling learner is conservative heuristics, not ASR fine-tuning; explicit
vocabulary entries remain editable and deletable.

This is original implementation based on the user's requested workflows. No
source code, application runtime, relay or model endpoint from the reference
dictation applications is included.
