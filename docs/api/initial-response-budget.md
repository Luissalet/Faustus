# Initial response budget

`POST /api/chat_stream` accepts the optional form or JSON field
`initial_response_timeout_s` (finite number, 5–900 seconds). It applies only
to this turn. Omitting it retains the selected endpoint's existing timeout;
saved preferences, reasoning effort, model and fallback policy are unchanged.
The form field is declared in OpenAPI for REST-to-MCP clients.

`scripts/faustus_run.py --initial-response-timeout 300` sends this field through
the same chat route. Native HTTP/MCP callers can supply it directly; it is not
a substitute inference adapter or a separate harness.

The initial budget includes same-chat admission, context preparation, shared
lane queueing and the first model response. A thinking delta or tool-call
starts the response. Subsequent generation retains the existing stream and
autonomy budgets. This change does not promise unlimited queueing or remove
the shared local lane. It does not add a saved chat/profile default or a new
composer control.

For an explicit override the response includes
`X-Faustus-Initial-Response-Timeout-S`. A detached turn emits
`initial_response_budget` with the configured seconds, remaining seconds and
scope. A timeout before the stream carries `X-Faustus-Timeout-Phase`:
`admission` or `preparation`. A streamed timeout includes `timeout_phase`:
`queue`, `preparation`, `admission` or `model`. The legacy error class
`first_token_timeout` is retained for existing clients; phase distinguishes a
queue timeout before any model call. Such failures do not trigger fallback.

No reasoning level is reduced to make a deadline. Model quality remains
unverified if a turn times out before a recorded model call or usable output.
