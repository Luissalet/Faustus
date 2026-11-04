# Streaming activity and long prefills

Faustus keeps normal chat and agent turns running after their browser subscriber
disconnects. Reconnect uses the existing run and replay cursor; it does not repeat
the original POST. Compare panes keep their existing direct cancellation behavior.

The OpenAI-compatible stream's `llm_stream_idle_timeout_s` (default 120, zero
disables it) starts with released content, reasoning, tool names/arguments, or
valid completion metadata. Empty lines, SSE comments, assistant-role frames and
empty JSON deltas neither start nor renew it. Silence after output produces
`timeout.inactivity`, status 504, with partial-output evidence and no automatic
transport replay. It is not evidence of degenerate model output and does not
request a model restart. Existing chat/agent continuation may continue from the
transcript; completed tools are retained rather than reexecuted. A reasoning-only
failure without visible answer text still surfaces the failure in plain chat.

Studio requires `[DONE]` or an explicit terminal/error/paused/cancelled event.
An EOF without one keeps received text and reports `transport.incomplete_stream`.
The final local `done` event only closes UI bookkeeping after that error.
Reconnect remains available through the existing run workflow. The embed/copilot
uses the separate TypeScript SDK, which already reconnects by cursor on incomplete
streams; changing Studio does not automatically update another app's SDK.

## Prefill budgets

The initial-response deadline is explicit, not inferred from context length.
`sparks_first_token_timeout_s` remains 300 by default. A turn/project override
`initial_response_timeout_s` (5–900 seconds) takes precedence. For managed Sparks
endpoints, the HTTP read allowance is at least this effective initial budget;
otherwise a normal chat's 300-second HTTP read could cut a configured 900-second
prefill short. Other endpoints retain their existing read timeout. An explicit
300-second initial budget is still 300. The mid-generation idle cap, cancellation,
queue ordering, and total turn deadline are separate and unchanged.

A measured 421-second prefill does not fit 300 seconds. For that service, an
operator can set `first_token_timeout_s: 900` through `PUT /api/sparks/settings` or
MCP `sparks_settings`, after deploying this change. The setting refreshes service
metadata through the existing sync operation; it does not reload the model. This
repository change does not apply that operational setting. A request/project
override can still choose 300. Nine hundred seconds allows that measurement; it
does not guarantee every future 1M-token prefill will fit.

The run heartbeat proves the Faustus run is alive. Model residency describes
memory. Neither establishes that this particular request is generating or gives
a measured prefill percentage. Studio labels waiting and estimated context size
accordingly. Engine timings/first-token latency available after output are not a
live progress meter. Admission/context preparation before the SSE response opens
still lacks streaming heartbeats; changing that requires a separate route design.

## Verification

Offline regression cases cover early role/empty frames, keepalives after real
output, continuous content/reasoning/tool arguments, incomplete EOF versus explicit
terminals, cancellation/cleanup, preserved continuation, and queue behavior.
Loopback HTTP tests scale the 421/300/900-second prefill comparison by 1:1000 and
verify the actual request's read timeout and single physical attempt. Private
API/MCP/browser fixtures exercise the production route, stream adapter and reducer;
they demonstrate transport behavior, not model quality or hardware stability.
