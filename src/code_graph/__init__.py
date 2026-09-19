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

__all__ = [
    "index", "search_graph", "trace_path", "callers", "callees",
    "detect_changes", "get_architecture", "snippet", "semantic_query",
    "impact", "cochanges", "change_risk",
]
