"""src/code_graph — architecture questions and call tracing over the
existing resolved code graph, without reading whole files.

R2 (Reach wave): the storage, extraction (Python `ast` with route/decorator
detection, call/import resolution with a `certainty` label, and the lexical
fallback for every other language via `src.repo_map`) and the incremental
sqlite index already exist in `src.context_engine.code_index` — see that
module's own docstring for why. Duplicating extraction or storage here would
be exactly the mistake the wave's contract warns against ("amplía, no
dupliques"): this package is a thin, read-only query layer on top of that
graph, adding the questions it does not yet answer as public functions —
`trace_path` (a resolvable path between two named symbols, not just a
one-hop neighbourhood), `detect_changes` (git diff -> affected symbols ->
their direct callers), `get_architecture` (languages, routes, fan-in/out,
hotspots — a repo's shape in one call) and `snippet` (exactly one symbol's
lines, nothing else). `search_graph`/`callers`/`callees` are compact,
budgeted renderings of `code_index.search`/`neighbors` that already exist.

`src.code_index` (top-level, lexical `find_symbol`/`callers`/`tests_for`) is
a *different* module with a documented, deliberate reason to stay separate
from `context_engine.code_index` (see its own docstring) — this package
builds on the resolved graph, not the lexical one, because `trace_path` and
`detect_changes` need edges with a certainty label to be honest about what
is observed versus inferred.

Code graph+ (this lot): two questions the above still could not answer.
`communities` (`src.code_graph.communities`) groups the workspace's files
into the modules a person would actually name, via a deterministic two-level
Louvain clustering over a weighted call/import graph (falling back to
directory grouping past a size/time budget). `flows` (`src.code_graph.flows`)
walks outward from every real entry point (routes, agent-tool executors, MCP
tools, public roots) along resolved calls to find the execution paths that
exist in this codebase, each with a deterministic 0..1 criticality score.
Both persist their results keyed by the index's own fingerprint, so a second
call with an unchanged index is a cache read. `impact` gains an additive
`affected_flows` key computed from the same seeds it already has; it can be
an empty list when nothing matched, but the key is absent entirely if
computing it failed (logged at debug), never presented as a false empty.
"""
from .query import (
    index,
    search_graph,
    trace_path,
    callers,
    callees,
    detect_changes,
    get_architecture,
    snippet,
    impact,
)
from .semantic import semantic_query
from .cochange import cochanges
from .risk import change_risk
from .communities import communities, community, community_of
from .flows import flows, flow, affected_flows
from .drift import snapshot, drift, list_baselines

__all__ = [
    "index", "search_graph", "trace_path", "callers", "callees",
    "detect_changes", "get_architecture", "snippet", "semantic_query",
    "impact", "cochanges", "change_risk",
    "communities", "community", "community_of",
    "flows", "flow", "affected_flows",
    "snapshot", "drift", "list_baselines",
]
