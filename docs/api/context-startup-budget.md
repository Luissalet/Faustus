# Context source startup and retrieval budget

The context compiler checks sections and cheap request gates before initializing
an optional source. Importing `services` no longer initializes document/vector
backends: its public exports are loaded from the same service modules when
requested, preserving class identity, `__all__` and their required side effects.

Each retrieval source has one deadline shared by availability and search (or
availability and exact-reference fetch). A
slow availability import therefore consumes its source's retrieval budget;
it cannot freeze the event loop before the search timer starts. A timeout is
reported with its phase and the packet records degradation rather than
pretending the unavailable evidence was retrieved.

Availability probes use a bounded process-wide pool: four daemon workers and
at most 32 outstanding probes, including queued work. A probe is reused while
pending for the same source instance and captured ContextVar identities.
The context copy preserves scoped providers such as live chat history; two
different captured scopes cannot borrow each other's availability result.
When capacity is full, a source reports that condition rather than starting
another thread or adding an unlimited queue.

Cancellation or timeout detaches the waiter, not the synchronous import.
The outstanding probe retains its slot until it finishes, and finalization is
audited, including completion after the waiter left. Python cannot kill a
blocked import thread safely. A probe that never returns can remain pending
until process shutdown; subsequent equivalent probes reuse it, and capacity
remains bounded. The workers are daemon threads so they do not make such an
import a permanent process-exit wait. This does not change existing search
worker lifetimes or claim to cancel their external I/O.

## Native diagnostics

`GET /api/context/diagnostics` and MCP `context_diagnostics` include
`compiler.availability`: submitted, reused, completed, failed, waiter timeouts,
waiter cancellations, late completions, rejected, pending, running, capacity
and worker counts. These are process-wide aggregate numbers. They expose no
source content, source identifiers, owner identity or captured ContextVar data.

The MCP compile/explain recall fields from the private-recall fix remain strict
JSON booleans. Strings (`"true"`, `"false"`), numbers, null, arrays and objects
are rejected; omitting a field uses its documented default. Project proof
stays available when private recall is disabled. A graph node without owner
metadata is taken only from the graph built for the request's fixed owner;
known foreign-owner or foreign-project metadata remains excluded.

## Verification

Tests cover blocked availability, a responsive concurrent health/API request,
the shared deadline, timeout reuse, cancellation, bounded capacity, completion
after detachment, recovery, scoped live history, mixed ContextVars, legacy
service exports and backend initialization on explicit access.

Native stdio MCP starts without NumPy pre-imports or source doubles. Synthetic
stores show private memory included with recall enabled and excluded with it
disabled, while the verified project experience remains. Optional Chroma is
absent in the QA profile: it is reported as unavailable/timeout rather than
requiring a service or model to be started. A diagnostic snapshot can still
show a pending optional import; that is reported, not claimed to be canceled.

The normal app frontend and native context API are checked in an isolated
profile. No production service, model configuration or user data is changed.
