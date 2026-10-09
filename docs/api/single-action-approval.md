# One-action permission in chat

The permission card offers **Only this action** (`approve_action`) as its narrowest choice. It consumes the
existing opaque pending approval and executes that sealed tool, input, owner, chat, workspace and document
version at most once. It does not authorize subsequent actions, save a chat permission or create a folder grant.
When a later call needs permission, the normal gate presents another card. The previous permission remains in
history; an unresolved new card takes precedence over that record in the turn summary.

Existing choices retain their meaning:

| Decision | Scope |
| --- | --- |
| `approve_action` | One exact action; later calls meet the ordinary gate. |
| `approve_task` | The remainder of the resumed request. |
| `approve` | The chat session. |
| `approve_workspace` | The workspace folder, including later chats. |
| `deny` | Refuse the proposed action. |

The same decision works with JSON or form POST `/api/chat_stream`: supply `session`, `message` (empty for a
continuation), `tool_approval_id` and `tool_approval_decision=approve_action`. Other active turn restrictions
still apply. The JSON OpenAPI contract documents the decision enum; the normal REST-to-MCP bridge carries it
to the same route. Its generic `body` argument is used when the operation exceeds the bridge's scalar-parameter
limit. The public TypeScript SDK accepts `approve_action` in `ToolApprovalDecision`; Studio shows the label and
consequence in English and Spanish.

An approval is checked against its owner/chat and absolute expiry before consumption. A consumed or superseded
ID cannot authorize a replay. The execution boundary rechecks the sealed arguments, workspace and document
version/digest; read-only mode, tool availability, desktop confirmation and destructive-command checks are
not weakened. `SINGLE_ACTION` grants always carry `allow_remaining_actions=False`, including grants constructed
by internal callers. Non-chat callers using `allow_continuation=False` keep their existing one-use behavior.

This restores a missing permission choice. It does not classify unknown MCP mutations as safe or solve native
project coordination by itself. Routine authorized coordination still needs its own scoped capability contract.

Verification covers the real loop executing one seal then generating a fresh card, HTTP owner/session/expiry/
supersession/replay rejection, stored history without a broader grant, existing task/chat/folder behavior, SDK
types and card rendering. Isolated native-app QA with a deterministic HTTP model verifies Studio and stdio MCP
through the normal API without GPU loads or user-data changes. Synthetic execution checks coordination mechanics,
not the autonomous quality of a real language model.
