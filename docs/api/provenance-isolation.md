# Private recall and provenance

`no_memory: true` on a chat turn (or incognito) reaches the Context Engine as
`policy.allow_personal_memory: false`. It disables private recall, including
learned-memory and historical-chat nodes returned by the provenance graph.
Those nodes cannot become permitted just because a graph labels the section
`past_experiences` or presents their source type as `experience`.

The graph builder accepts `include_personal_memory` (default `true` for existing
audit callers). When false, it never consults the learned-memory store. Private
memory/chat nodes and their edges are absent from the result. The context
adapter also filters mixed or stale graphs before explaining a checkpoint,
keeps ownership/project metadata, and separates its cache by owner, workspace,
project and recall policy. Direct `prov:memory:*` and `prov:chat:*` fetches are
refused before building a graph when private recall is off. Ranking applies
the same refusal even if a producer incorrectly calls the item an experience.

This preserves project evidence. Observed project checkpoints and separately
admitted, verified `experience:*` records remain available within their owner
and project scope. Disabling private memory is not an instruction to remove
the current transcript, project rules or all previous project verification.

`no_skills` independently suppresses skill-store discovery/injection. It does
not make private memory eligible: a remembered reference to a skill is still
private recall and is excluded when `no_memory` is true. Explicit project
instructions and tool documentation retain their existing contracts.

## MCP

Native `context_compile` and `context_explain` accept two optional, strictly
boolean fields:

- `allow_personal_memory` (default true): false reproduces the private-recall
  policy of `no_memory`/incognito.
- `allow_semantic_lane` (default true): false disables semantic retrieval while
  leaving exact, lexical and graph lanes available.

The configured MCP owner remains authoritative; a caller cannot replace it
through arguments. String values such as `"false"` are rejected rather than
coerced to true. These tools return manifests/diagnostics, not whole prompts.

## Verification and limits

Synthetic regression checks cover mixed graphs, private exact references,
policy changes in a warm cache, cross-owner/project records, edge explanations,
zero memory-store reads and preserved project experiences. Existing chat
control tests verify that Form/turn flags reach live/shadow request policy and
the legacy prompt builder. The separate JSON request parsing fix belongs to
the initial-response-budget change; it does not repair a graph-origin bypass.

Native stdio MCP is checked against synthetic stores, without model calls.
The standalone cold MCP process also exposed an independent initialization
stall while importing NumPy through an availability probe. Policy verification
with NumPy imported before the transport is warm evidence only; it does not
certify cold startup. That startup defect is tracked separately rather than
hidden by a test bootstrap. The normal isolated frontend loads unchanged.

The private flag does not redact text that the runtime explicitly supplies as
current instructions or that an author has placed inside a project proof.
It prevents private retrieval; it is not a general document-redaction service.
