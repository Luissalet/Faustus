# Read-only connector tools

The read-only autonomy preset can discover and execute a connected MCP tool
whose provider explicitly declares `readOnlyHint: true` and does not declare
`destructiveHint: true`. Unknown, unannotated, disconnected and mixed/write
tools remain excluded. Names such as `get_...` do not establish permission.

The classification is a mediated private/network read with an untrusted
result. It does not change the connector allowlist, disabled-tool settings,
per-call approval or the local no-network carve-out. Discovery and execution
consult the same live classification, including revocation after disconnect.

Exact catalog lookup resolves a short MCP name when one permitted provider
matches. If several permitted providers match, `ambiguous` contains qualified
choices and those tools are not promoted until a qualified choice is requested.
Tools excluded by the current policy are not included in the ambiguity list.

This enables an agent to inspect coordination tasks from the connected Hub
without granting claim, post, checkpoint or other writes to a read-only turn.
Those actions need an explicitly authorised working profile. A connector being
connected is not evidence that an agent has actually used it: validation must
include the model's tool call and the server's result.
