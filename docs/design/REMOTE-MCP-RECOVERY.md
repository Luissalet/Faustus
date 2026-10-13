# Remote MCP connection recovery

Faustus keeps HTTP and SSE MCP transport/session contexts in dedicated owner
tasks, as it already does for stdio. The owner enters and exits the SDK contexts;
other tasks request closure through its event. Cancelled initialization retires
the owner instead of leaving an orphan connection or raising a teardown
cancellation as a new caller cancellation.

After a configured remote server disconnects, a tool request that was provably
never sent can reconnect and run once. An annotated read can also reconnect after
a transport loss, session termination or its existing RPC timeout. Writes whose
response was lost retain outcome_unknown and are never blindly replayed. A later
call can retry an unavailable remote connection when the same enabled server
comes back. Changing the endpoint prevents an old call from being replayed at
the new endpoint.

Recovery reuses the authenticated OAuth provider for the same configured
endpoint. Public HTTP servers that never challenge for OAuth do not receive a
token preflight demanding reauthorization. A slow initial connection without an
authorization URL stays connecting; actual authorization still uses the existing
needs_auth/callback workflow. Recovery changes no permission or approval policy.

There is one retry of the affected call, not a restart of Faustus or healthy MCP
servers. This is on-demand recovery, not an always-on reconnect scheduler. Tool
schemas and connection identity are compared before replay; a changed tool
contract asks the caller to use the new catalogue. The existing MCP call timeout
still applies (180 seconds by default); no new execution limit is introduced.

## Verification

Real private FastMCP HTTP and SSE servers exercise connection ownership, normal
closure, server restart, server-down then return, lost write responses, read
recovery and endpoint changes. An HTTP case persists a synthetic OAuth token and
proves the same provider/token is retained. Pending initialization cancellation
also verifies owner cleanup. The fixture RPC timeout is two seconds solely to
avoid spending 180 seconds on the deliberately stopped server.

Native QA uses the ordinary execute_tool_block -> McpManager -> SDK path, with
the native /api/mcp/tools catalogue, four tools and two private services. Both
services answer with the new generation after restart; the browser confirms the
same flow. These are fixture services with real transports, not external accounts
or a full Studio/model quality benchmark. No application bootstrap, shared
vector store, production data or model server is part of this QA.
