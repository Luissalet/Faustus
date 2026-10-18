# Unattended completion audit

An unattended turn with a bound workspace can still be a question or an
analysis of supplied data. A completed answer does not require another model
call merely because that folder is present.

The work audit now requires positive evidence: workspace work recognized by
the existing request preparation, a requested file deliverable, actual ledger
effects, an active progress list, or a tool call with the canonical
READ_WORKSPACE capability. The prepared request result retains its
`workspace_work` flag, including explicit continuations and attached
implementation instructions. The closing stage reuses it instead of scanning
the complete request again. The no-action nudge uses the same evidence in
attended turns, including MCP, and unattended turns, so it cannot replace a
completed data answer with another forced round. A reply that only echoes a
context boundary still requires recovery even without workspace work.
The existing conversation, calculation and explanation exceptions remain.

This changes eligibility only. The audit still runs once for real work,
including a job that has not produced its requested file yet. Returning JSON
does not exempt a request to write a file, nor does it erase effects already
performed. Relay delivery, round availability, the unattended audit policy
and downstream effect, syntax, test, UI and pending-plan checks retain their existing flow.
There is no early successful return around those checks.

The workspace classifier also distinguishes rooted paths from division or
other slash notation inside a word. Absolute `/...`, home `~/...`, explicit
relative `./...` and `../...`, drive paths and UNC paths must start a token.
Existing recognized filename extensions remain targets. An extensionless
`word/word` reference requires an explicit file/folder target or a direct
filesystem operation. For example, `Read the ratio a/b` is an answer request;
`Read a/b` is a direct path operation. `Edit config/settings`,
`Lee la carpeta foo/bar`, `/etc/hosts` and `C:\work\LICENSE` remain supported.
This is a deterministic routing hint, not a general language parser.

Direct relative-path operations use the existing workspace action vocabulary,
so code operations such as `Fix src/widgets`, `Refactor src/widgets` and
`Review config/settings` keep their work obligations even without a filename
extension. A separate partial verb list would silently lose those obligations.
The path must still be the direct operand; `Inspect the ratio a/b` does not
become a filesystem request.

The regression tests drive `stream_agent_loop` with scripted provider SSE.
A valid nonempty JSON STOP response is followed by a provider error that must
never be requested. Both direct STOP and LENGTH followed by STOP end without
an audit or no-action round. Contrasts preserve file jobs with JSON summaries,
real writes in temporary workspaces, and the existing syntax-repair path.

The normal MCP facade is also exercised with its existing attended agent
payload and a bound workspace. Complete data answers, including multiline
output, survive unchanged with one provider STOP. Attended file jobs still
require action, and context-boundary echoes still require a real answer in
both attended and unattended turns.
The original full private request is checked separately by the unchanged
classifier helpers before and after the change; it is not copied into tests.

These are CPU tests of native loop behavior with a simulated provider. They
do not certify a new real-model run, API/browser acceptance, health latency,
wire integrity of the historical model request, or model coherence. Those
checks remain separate from this change.
