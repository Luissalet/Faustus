# Delegation continuity and usable reports

`delegate_agents` runs through the existing chat, API and MCP entry points.
No external agent runtime, permission system or additional model is required.

## Behavior

- Workers inherit the coordinator's endpoint, headers and model. Explicit task
  `model`/`endpoint_id` and configured tiers remain available, including the
  global Sparks service, and are advertised in the native tool schema. An
  endpoint chosen without a model uses its own configured model and credentials.
  The existing unavailable-route fallback appears in the report. Selection
  does not replace or unload a served model.
- Omitted deadlines and `timeout_s: 0` have no time limit. A positive job or task
  deadline is honored exactly, measured after queue admission. A task's zero
  can override a job deadline. Definitions that explicitly request a deadline
  retain their existing contract.
- `agent_subagent_max_tasks: 0` accepts the full task list. Positive configured
  widths still apply and report dropped tasks. Endpoint and team concurrency
  remain independent; accepting tasks does not start all generations at once.
- The deterministic supervisor is disabled by default. Its existing opt-in
  behavior, manual Stop, parent cancellation and file concurrency ownership
  remain available. Existing saved choices are preserved.
- Reading and research return a `reported` receipt, distinct from `produced`
  file edits. Failed reads and acknowledgement-only replies are not reports.
  A read mentioning a filename does not trigger an instruction to write it.
  Successful non-file tools can report their result without inventing local
  mutations. Requested source edits still need recorded mutations, including
  implementation tasks naming a backend/API rather than a literal path.
  Assigned files supply additional context; an input CSV for a calendar action
  does not become a file to rewrite. Explicit read-only reviews never trigger
  the write-oriented retry.
- A worker's final report starts after its last tool result, so guessed
  pre-result narration is not presented as the result. Native response
  replacements are consumed. Round activity and tool outcomes stay available.

## Evidence and limits

Regressions reproduced hidden deadline conversion, idle cancellation, discarded
tasks, read-to-write retries and ignored response replacements before correction.
The existing delegation, Stop, locks, definitions, effort and settings tests
cover retained behavior.

A private native chat route called the real tool executor, delegation runtime,
worker loop and HTTP transport. Two synthetic models read a real private file;
closing the native copilot MCP observer still allowed both workers to finish.
The previous source cancelled both workers under the same accelerated idle
threshold; the corrected source completed both. The acceleration makes this a
deterministic recovery test, not a model performance benchmark. Reading no
longer caused an extra write-oriented attempt.

Two roles also ran against the already served global GLM model on Sparks, through
native stdio MCP and the chat route. Both read `Valor comprobable: 42`, completed
with one successful read each, changed no files and returned the correct final
value after observer disconnection. Earlier runs exposed an incorrect
intermediate model claim; the evidence was retained, and the report consumer was
corrected rather than counting that initial output as flawless.

One real model was configured for this evaluation. Different model routing was
tested with synthetic endpoints; no second real model or universal quality,
speed or long-task superiority is claimed. Persistent group orchestration is
tracked separately in task 443.
