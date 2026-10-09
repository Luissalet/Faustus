# Scoped native heartbeat

`coordination_heartbeat` reports activity even when its fixed actor also owns locks outside this project's binding. The native client reads current locks from the bound connector, derives an exact renewal list using the binding's existing resource rules, and sends that list to the Hub. Model arguments can set doing/state/note; they cannot choose renewal resources, actor, connector, force or grants.

Outside owned locks generate a count-only `locks_outside_binding` advisory. Their names are not disclosed by that notice and their TTLs are not extended. Foreign locks are untouched. An empty selection records metadata without renewal. Another controller may renew its own reservations separately; no model or resource name is hardcoded as an exception.

The live Hub must advertise `locks.capabilities.heartbeat_selective_renewal: true`, and its heartbeat MCP schema must offer the array selector. This protects a mixed deployment where local MCP catalogue code is new but the running Hub still has legacy all-lock behaviour. Missing protocol support explains the update/refresh required; it never asks for owner approval or silently widens renewal. After dispatch, a missing or mismatched selected-renewal receipt is an unknown outcome, requiring inspection before retry.

Read-only policy, disabled tools, actor identity, binding/connector revalidation and revocation handling retain their existing protections. This changes ordinary out-of-scope lock bookkeeping, not permissions or approval policy. Heartbeat remains a metadata write and is unavailable in a read-only turn.

API `/api/coordination/{session_id}/actions`, the normal chat tool and REST MCP expose the same result and advisory. Project/template identity and resources come from the normal native configuration. Validation records actual lock expiries in an isolated Hub and uses a GPU-free HTTP provider and Studio; it does not benchmark an autonomous model.
