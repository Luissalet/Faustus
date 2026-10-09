# Project execution defaults

Projects can save four optional preferences for their streaming conversations:
`autonomy_preset`, `reasoning_effort`, `thinking_enabled`, and
`initial_response_timeout_s`. Model selection, endpoint and context capacity are
unchanged. These preferences do not grant tool access or approval scope.

`PATCH /api/projects/{project_id}` accepts an `execution_defaults` object and
`execution_defaults_expected_revision`. Updates are owner-scoped and atomic.
The revision starts at zero; stale or absent expected revisions return 409.
Omitted fields stay unchanged; an individual `null` removes that preference.
Booleans must be actual booleans, known enums must be exact, and the initial
wait must be a finite number from 5 to 900 seconds. Reads include the saved
object and `execution_defaults_revision`. Ordinary project edits preserve both.

`GET /api/projects/execution-defaults/session/{session_id}` verifies session
ownership and returns its active project's defaults, project id and revision.
An unrelated, disabled or absent project contributes no defaults. The native
REST-MCP bridge exposes this read and the same project PATCH schema.

The `POST /api/chat_stream` path resolves preferences once for that request.
Explicit turn fields win over project preferences, which win over existing
application defaults. Existing explicit reasoning modes and generation
overrides remain stronger than inherited reasoning/thinking. Explicit `false`
and `auto` are retained. The initial wait covers admission, preparation, queue
and the first meaningful response, then resets when the request finishes;
detached tasks retain their own context. Headers expose the applied wait and
project preference revision. Requests without a configured project keep their
existing defaults. Non-streaming `/api/chat` is unchanged.

Studio displays preferences for the next turn. The project form combines
reasoning level and Thinking into one choice: selecting a level enables
Thinking; Off disables it. Changing only the wait or autonomy preserves an
unchanged saved reasoning pair. Per-chat adjustments remain keyed by session,
with no write to the browser's global autonomy knobs; clearing them restores
inheritance. The first normal composer send can omit all four fields and still
use the saved project settings. Choosing an existing override before a new
chat gets an id carries it into that new id.

The composer and sender share the same autonomy precedence: per-chat choice,
then a valid project preference, then the browser's existing global choice.
When the project has no autonomy preference, Studio explicitly sends that
global fallback, including `read_only`; it never displays read-only while
silently requesting supervised execution. A valid inherited project preference
still stays absent from the request body and is resolved by the server.
Studio blocks click and keyboard sends while project resolution is pending or
failed, keeps the draft, and offers Retry on failure. The first new-session
send and other dispatch paths also resolve preferences before starting a turn.

The displayed context window comes from endpoint metadata, independent of the
reasoning budget and input token budget. A reported 1M window is not a load
instruction or proof of a million-token inference. GPU-free scripted HTTP
models verify forwarding; autonomous quality on GLM requires a separate live
evaluation. Disconnected or disabled coordination does not erase preferences.
