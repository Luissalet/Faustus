# Literal shell commands

Type `!!command` in Studio to execute one literal shell command without a model call. The command and its output are saved in the conversation. A new conversation can be created without a configured model. `/help` lists the prefix; the existing `/sh` command is unchanged.

The call uses the ordinary `bash` tool executor, its platform routing, working directory, command guard, sandbox and global tool policy. A project's read-only preference and the stricter per-call read-only choice remain effective. The server resolves and validates the directory. Output is marked as untrusted tool output for later model turns. This feature does not make an unrestricted host shell safe: the execution target and sandbox policy are the same ones the bash tool reports.

## HTTP

`POST /api/shell/command` accepts:

```json
{"command":"pwd","session_id":"optional-owned-session","workspace":"optional-working-directory","project_id":"optional-for-a-new-session","autonomy_preset":"supervised"}
```

The result contains `session_id`, the normal executor `result` (output/error, exit code, execution target and any denial) and nullable `approval_id`. Without a session id, the server creates a model-free conversation. With a session id, it must belong to the caller. The project supplies its directory when no explicit directory is given. Both cookie and API-token callers must meet the existing dispatch administrator rule; tokens need `agents:dispatch` and an admin owner.

`GET /api/shell/command/pending?session_id=...` returns the caller's pending literal command or null. This restores the confirmation after a page reload. It never exposes a different owner's command or consumes a model turn's approval.

`POST /api/shell/command/approval` accepts `{ "approval_id": "...", "decision": "approve_action" }` or `deny`. The existing approval store seals the exact owner, session, command and directory. Approval is single-use and policy is checked again before execution. It never grants task, chat or workspace scope. Internal tool-originated requests cannot answer it. Studio offers **Allow once** and **Deny**; declining does not dispatch the command. An expired or consumed approval must be requested again.

## MCP

The workers server exposes `shell_command` with the same execution fields. It returns the result as JSON, including the conversation link and any approval id. Its token uses the existing `fable_workers`/dispatch authority. The tool does not approve itself; approval is a separate direct client action in Studio or the HTTP confirmation endpoint.

Calls are synchronous. Closing a tab or losing the response does not establish whether a command stopped; check the saved conversation before repeating a command that can change files. The client does not automatically retry this POST. Ordinary bash timeouts and idle limits still apply.
