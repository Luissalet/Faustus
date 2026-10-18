# MCP tool deadlines

Faustus preserves the catalog extension `x-timeout-s` through tool discovery,
pagination, refresh and the in-process catalog cache. The value is seconds for
one blocking tool call. A top-level value takes precedence over the existing
`inputSchema` extension. It must be a positive finite JSON number representable
as the MCP SDK's `timedelta`; booleans, strings, containers and invalid numbers
fall back to the existing 180-second limit. Changing a hint invalidates the
catalog version even if the server keeps its version string.

A valid hint sets both the official `ClientSession.call_tool` request timeout
(`read_timeout_seconds`) and the enclosing `asyncio.wait_for` deadline. Progress
and logging notifications do not renew that total deadline. Without a hint the
180-second default is unchanged. An explicitly configured
`ODYSSEUS_MCP_CALL_TIMEOUT` remains a ceiling, including a shorter value. This
change does not infer time units from arguments or impose a new upper limit on
valid declared values such as 900 or 14,400 seconds.

Native catalog hints cover the existing blocking observer budgets:

| Tool | Hint (seconds) | Existing source of the budget |
| --- | ---: | --- |
| `workers_wait`, `workers_wait_for` | 615 | maximum wait 600 + HTTP result margin 15 |
| `todo_assign_agent` | 693 | initial POST 30 + wait loop 600 + final sleep 3 + last GET and its read retry 60 |
| published `wf_*` | 360 unless explicitly declared | maximum wait 300 + request budget 30 + result margin 30 |
| `voice_session` | 1080 | wait 600 + final event read 120 + status/send/final status reads 360 |

These hints allow a valid wait to finish; they do not promise that a stalled
network operation or repeated reads will always finish within that time. For
example, the workers bridge retains its existing GET retry, and the hint does
not allocate another complete 600-second wait for that retry. HTTP read
timeouts also measure stalled reads rather than an entire streamed response.
The server still applies the particular wait requested by the caller.
An unfinished worker or workflow returns its persistent job/run identity;
observe that identity again instead of dispatching the same work again.

Cancellation propagates, and enclosing agent-turn deadlines still apply. A
lost response to a dispatched write remains `outcome_unknown`; increasing the
observation budget does not grant permission to replay it. The registration,
timeout and replay authority belong to the same captured tool binding.

Private regression tests use the native workers MCP server over real stdio and
real loopback HTTP with clocks uniformly scaled. They cover 300/600-second
waits, 900/14,400-second catalog hints, explicit shorter limits, continuous
notifications, cancellation and single-request behavior. They make no model
calls and do not establish model quality or production network reliability.

Hoard Hub's own MCP-to-HTTP deadline and catalog bridge are separate consumers
(task 851). A hint reaching Faustus does not fix an earlier bridge that discards
it or times out first. Likewise this change does not enable optional SDK
streaming in every worker, alter model first-token settings, or change the
short explicit budgets of typed choices and workflow model nodes.
