# Approval inbox

The inbox is a view over `ToolApprovalStore` and the existing contract approval
store. It creates no additional approval points and changes no global approval
mode. Studio exposes `/approvals`, linked by a bell in the sidebar.

Each pending request shows a stable four-character code, an effect-derived level
(trivial, low, medium or high), its age, origin, and the proposed action. Approve
and Deny use the native one-use decision. A tool approval resumes its original
session through `/api/chat_stream`, with `approve_action`, the sealed workspace
and input, current tool availability and the usual persisted timeline. Contract
cards retain native grant/deny semantics; their consumer executes the plan.

Codes use uppercase letters and digits without 0/O or 1/I. Assignments persist
across restarts, resolve collisions without changing earlier codes, and are not
reused for another card. Previously delivered ten-character codes still work.
Studio uses the chat approval card treatment, colored level chips, origin icons
and conversation/task names, a visible action summary, shared primary/secondary
buttons, and a collapsible remembered-answer section below the requests.

Scheduled assistants and automations retain their existing exact-action cards
instead of discarding them. The continuation resumes the parked action in the
task's chat, not a rerun of the entire scheduled mission. The original TaskRun
still records its paused result; subsequent continuation output belongs to the
same chat. Arbitrary conversational questions are not executable approval cards.

## Phone replies

The paired, already-running WhatsApp bridge sends requests only to the account's
self-chat. It neither starts nor pairs a bridge, and never sends to contacts or
groups. Literal replies are parsed with standard-library rules:

- `<code> sí` / `<code> no` (also `yes` / `no`).
- `sí` / `no` alone when exactly one request is pending, already delivered to
  that channel. Multiple pending requests require a code.
- `<code> siempre sí` / `<code> siempre no` (also prefix order and English).
- `sí a todo` / `yes all`: the snapshot of still-pending codes delivered there.

Free prose does not decide anything. Only original, unedited own messages in
that self-chat are accepted. The delivery and processed-message state survives
restart; the native store consumes each tool grant once. Old, expired,
superseded, foreign-owner or repeated replies cannot execute another action.
Responses are read before newly arrived cards are delivered, so “all” cannot
include a request first sent during that poll. The bridge's retained last 200
messages (within 24 hours) bound catch-up; this is not a full phone history sync.
Parsing a decision makes no model call. Continuing the approved task may use
the configured model as normal.

Telegram's existing bridge also accepts literal replies in an already-allowed
private chat, scoped to that chat's mapped session. Existing group chat turns
and channel connections retain their behavior; groups cannot answer decisions.

## Remembered answers

“Always” remembers **the same tool, complete input, effects, document identity
and workspace**, under the same owner. A different recipient, command, document
version or folder is a different pattern. The inbox applies a remembered answer
only after the existing machinery has produced a pending card; it never widens
the global gate or task/chat approval scope. A remembered denial uses native
deny. Studio can switch a remembered answer or forget it.

`approval_inbox.json` stores code assignments, patterns, channel delivery state and scheduled
origins under the normal data directory. Pending tool cards retain their existing
store/persistence/expiry. No duplicate approval database is introduced.

## API and MCP

- `GET /api/approval-inbox`: caller-owned pending cards and remembered answers.
  Bearer tokens use the existing `sessions` scope and their own owner.
- `POST /api/approval-inbox/{code}/decide`: `{granted: boolean, remember?: boolean}`.
- `PUT /api/approval-inbox/rules/{pattern}`: `{granted: boolean}`.
- `DELETE /api/approval-inbox/rules/{pattern}`: forget an answer.

Decision and rule writes retain the existing `require_human` boundary. Reads
are available to the tool layer. The stdio server
`python -m mcp_servers.approval_inbox_server` exposes `approval_inbox`, a read-only
tool with codes, levels, age, origin and remembered patterns. It uses
`FAUSTUS_URL` / `FAUSTUS_INTERNAL_API_URL`, and the existing token environment
variables. Reading a card never grants it.

Candidate for task mejoras #3120: integration, service restart and Spark
deployment belong to the reviewer. Source presence does not activate the new
router or channel loop in an already-running service.
