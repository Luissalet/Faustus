# Code references and change impact

Python call resolution follows explicit import bindings before considering
unqualified name matches. Aliases belong to their module or function scope;
parameters and local assignments can shadow them. Calls to the same function
name through different modules remain separate. A binding to a missing or
external module does not become a call to a local namesake.

The existing `static_inferred` certainty remains a static inference. Dynamic
dispatch, runtime rebinding, wildcard imports and arbitrary re-exports are not
proof of an exact call. Ambiguous calls without corroboration remain unresolved.

The derived SQLite index retains source references and their file hashes.
Adding, changing or removing a definition re-resolves references from unchanged
callers without reparsing their source. This also removes stale inferred calls
when a previously unique name becomes ambiguous. Existing indexes backfill
reference records as files are visited; a targeted refresh preserves unvisited
legacy edges until their files are refreshed. A complete refresh backfills all
visited files. A truncated walk still reports its existing `truncated` flag.

## Native interfaces

- Chat and MCP: `code_graph_impact`, with `symbol`, `root` and optional `depth`.
- HTTP: `GET /api/code-graph/impact?symbol=place_order&root=<workspace>`.
- Omit `symbol` and pass `base_ref` to seed the existing analysis from a git diff.
- `include_history=false` and `include_risk=false` request structural impact
  without optional history or risk computation, useful for folders outside git.

Both interfaces return the existing reached consumers, certainty, affected tests
and suggested command. The HTTP route uses the same administrator and workspace
rules as the other code graph routes. No hosted index, model call or external
API is needed. Refreshing does not alter source files.

Methodological source inspected: Graphify-Labs/graphify,
86c57be8204673c2a1b9849a3292a7dd3ab373d8. The implementation extends the native
index and existing impact traversal; it does not execute an external graph CLI.
