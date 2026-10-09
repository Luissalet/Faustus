# Native project inheritance from the CLI

`scripts/faustus_run.py` uses the normal application APIs and `/api/chat_stream`. A new chat can select an owned, enabled project with `--project-id`. Without that option, a supplied `--workspace` that matches exactly one enabled project inherits it. Matching is canonical and exact, not a parent-directory guess. With multiple matches, the CLI warns and continues with the supplied workspace without choosing a project. `--project-id` resolves that ambiguity explicitly. A legacy server or token that cannot list projects keeps the legacy workspace flow with a warning.

`POST /api/session` now accepts optional form field `project_id`. The owner-scoped project lookup and enabled check happen before creation; `SessionManager.create_session` persists both the stable project ID and its sidebar folder in the original creation. Invalid, disabled or foreign projects do not create a chat. The CLI verifies the returned chat's native project resolution before inference. This avoids attaching by folder or inventing a binding.

Project execution defaults, workspace/context roots and the saved coordination template are resolved by the server exactly as they are for Studio. The CLI does not copy the four project execution values into its turn request. Explicit options such as `--effort` and `--initial-response-timeout` remain per-turn overrides. It does not change approval settings, project defaults, grants or connector scope.

Resuming `--session` preserves its project and actor. An optional `--project-id` must match the current project; it cannot move the chat. The CLI reports the project observed from the server. Automatic identities remain native, stable per chat and different between chats.

For an external worker that needs a named Faustus identity, optional `--agent-id faustus` (or another valid Faustus-prefixed ID) configures only a new project chat through the existing owner-managed coordination API. It copies exactly the active template's connector, operations, resources, repositories and threads, and lets the API validate and persist the actor. Human identities such as `luis` and other agent identities are refused. A resumed chat's existing actor cannot be replaced; if an explicit saved actor already matches, it is retained. The CLI sends no actor/grant fields in the chat form and forges no model context; the native runtime supplies its saved binding. Existing API access requirements remain unchanged; local no-login mode adds no account or approval step.

Examples:

```powershell
python scripts/faustus_run.py --workspace D:/code/project -p "Review the project and run its tests"
python scripts/faustus_run.py --project-id <existing-project-id> -p "Review the pending work"
python scripts/faustus_run.py --session <existing-chat-id> -p "Continue"
```

Verification uses isolated Faustus and Hoard Hub instances with their real API/MCP implementations and a GPU-free HTTP model simulator. CLI workspace selection, explicit project selection and explicit owner identity each execute a native coordination heartbeat and a real Node test without approval cards. MCP chat and the normal Studio composer use the same configured project. Effective runtime values are high reasoning, Thinking enabled, a 300-second initial response budget and bounded autonomy, inherited without those fields in the CLI request. Resuming preserves the stored actor. The fixture declares 1M context metadata; this is not a real-model autonomy benchmark. The active external watcher and production chats/configuration are not modified by this change.
