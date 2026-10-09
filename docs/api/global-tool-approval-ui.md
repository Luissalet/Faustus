# Global tool approvals

The shell, Settings and Studio shortcut share one global approval control. It writes the existing persisted `tool_approval_mode` setting through `POST /api/auth/settings`; `GET /api/auth/settings` returns its confirmed value. There is no session-specific approval mode. The ordinary REST MCP bridge exposes the same API contract with an explicit JSON-body manifest for POST: the current settings OpenAPI operation does not declare its request body, so auto-discovery alone cannot send that write correctly.

| Value | Interface | Current behavior |
| --- | --- | --- |
| `ask` | Ask / Preguntar | Requests approval when the existing tool checks require it. |
| `auto` | Automatic / Automático | Applies those same checks today; some tools still request approval. |
| `full` | Full access / Acceso completo | Runs tools without approval prompts. |

This change does not redesign those backend rules. Full access does not add unavailable tools or change the model, workspace or execution configuration. Local no-login installations require no account or administrator role to use the control. It stays available while a conversation is busy; only loading and saving disable its choices.

Same-tab Settings saves notify the shared store. Other tabs refresh on a broadcast or storage signal. Focus, visibility and a three-second visible-page refresh also pick up changes made through the API or MCP. Failed saves retain the confirmed value and offer Retry. An older in-flight read cannot overwrite a confirmed save.

Verification uses an isolated local application and an HTTP model simulator, without GPU inference. Two native conversations each read external content, execute Bash with a HIGH declaration, run a real Node test and write through a real MCP stdio connector: five successful tools and zero approval cards per conversation in Full access. Ask and Automatic both stop at the same approval card after the read. This is approval plumbing evidence, not an autonomous-model capability benchmark. Browser synchronization, persistence and detailed evidence are recorded in the task's QA folder.
