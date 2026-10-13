# Continuing after temporary Sparks service outages

The shared streaming transport waits for the configured Sparks endpoint to
recover when it explicitly returns a retryable HTTP503 before any content.
The task keeps the same model, headers, prompt and completed tool results. A
failure after content remains handled by the existing partial-response logic.
Other endpoints retain their existing retry/fallback behavior.

No new agent loop or external runtime is introduced. Chat, agents, delegated
workers, persistent groups, Council interventions and the native copilot MCP use
the shared transport. Council selects one existing route; service recovery does
not switch the participant's model or create another intervention.
The existing continuation event tells observers that the task is waiting;
closing an observer does not cancel a detached turn. Retry intervals increase
to 30 seconds, without imposing a maximum attempt count. Completed actions are
not repeated while inference is unavailable.

Stop cancels the wait. An explicitly supplied initial response deadline still
applies. Authentication, configuration, non-retryable failures and partial
content errors are not silently held as temporary service outages. The check
uses the configured recipe's endpoint identity rather than treating every
private-network URL as Sparks. No model is loaded, stopped or changed.

## Verification

The previous source failed after a retryable service error. Controlled tests
recover after seven error cycles, retain a previous tool-call receipt, preserve
explicit deadlines, cancel during waiting and do not replay after content.
Failed-attempt metadata is not published as successful inference.

A private normal chat route ran the actual agent loop, executor, file tool and
HTTP transport. It created a file once, received seven HTTP503 failures on the
next inference step, then completed after nine total requests. Every retry
retained the completed tool call ID. Native copilot stdio (13 tools) started the
same flow; after its observer closed, another client resumed the same run and
received seven wait events, one successful write and the final response.

The first native attempt exposed that HTTP-status errors omit the partial
field; the classifier now combines that protocol with its observed pre-content
state. The initial failed receipt was preserved. No real Sparks outage was
induced and no production sessions or files were used. These are fault-tolerance
results, not a universal model-quality or performance ranking.

Council verification adds four cases: seven pre-content outages recovered on
the same route, partial content not replayed, an explicit intervention deadline
cancelling the wait, and a provider timeout without a room budget returning its
typed failure. The last case previously raised an exception while formatting
the absent duration. A private full-product Council turn through its native
12-tool MCP server completes two contributions after seven HTTP503 responses
and nine model requests, with the observer already closed. No real service
outage is induced.
