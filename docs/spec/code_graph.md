# Code graph (R2, Reach wave)

Architecture and call-tracing questions answered from a persistent,
per-workspace graph of symbols/calls/imports/routes — without reading whole
files.

## What already existed (reused, not duplicated)

- `src/repo_map.py` — per-language symbol extraction (Python `ast`; regex
  for JS/TS/Go/Rust/Java/Ruby/PHP/C/Swift) and the per-turn repo map text.
- `src/context_engine/code_index.py` — the actual resolved graph: Python
  extraction via `ast` (functions, classes, methods, constants, FastAPI-style
  route decorators `@router.get("/x")` → `kind="route"`, calls resolved by
  import/alias/bare-name, imports resolved to workspace modules), the
  lexical fallback for every other language via `repo_map`, incremental
  sqlite storage (`DATA_DIR`, WAL, one file per Context Engine store,
  `code_symbols`/`code_edges`/`code_files` tables scoped by
  workspace+project_id), hybrid lexical+semantic `search`, and `neighbors`
  (one-hop BFS with a `certainty` label per edge: `exact` / `static_inferred`
  / `lexical`).
- `src/code_index.py` (top-level) — a *different*, deliberately separate
  lexical index (`find_symbol`/`callers`/`tests_for` tools) — see its own
  docstring for why it is not merged with the above.

None of extraction or storage is duplicated here. `src/code_graph/` is a
thin, read-only query layer on top of `context_engine.code_index` that adds
the questions it did not yet answer as public functions, plus the
`code_graph_*` tools/routes the contract asks for.

## New in this lot

- `src/code_graph/query.py` — `index`, `search_graph`, `trace_path`
  (BFS over `calls`/`imports` edges between two named symbols, with
  file:line and certainty per hop, depth-capped at 8), `callers`/`callees`
  (compact renderings of `code_index.neighbors`), `detect_changes` (real
  `git diff` → changed line ranges → overlapping symbols → their direct
  callers, so a diff tells you what to re-test), `get_architecture`
  (languages, symbol/edge counts, HTTP routes, top fan-in/fan-out, hotspots
  = long functions with many callers) and `snippet` (exactly one symbol's
  lines). Every function confines its `root`/`workspace` through
  `src.tool_execution._resolve_search_root` (the same guard `read_file` and
  grep/glob use) and bounds its rendered `output` to ~4000 chars by default
  (`output_chars`/`limit`-tunable).
- `src/code_graph/semantic.py` — `semantic_query`, reusing
  `src.embeddings.FastEmbedClient` (the same fastembed wrapper
  `src.tool_index.ToolIndex`'s in-memory lane already builds from) as the
  embedder passed into `code_index.search`. Degrades to lexical search when
  fastembed is missing/broken — no second embedding stack.
- `src/code_graph/auto_index.py` — `maybe_auto_index(workspace)`: setting-
  gated (`agent_code_graph_auto_index`, default True), self-throttled
  (skips a workspace re-kicked within 5 minutes or already fresh), fires the
  actual (re)index in a background thread (`asyncio.to_thread` +
  `asyncio.create_task`, never awaited) so a code turn is never blocked on
  it. Wired into `src/agent_loop.py` with a single try/except-guarded call
  right where the loop already gates on "workspace bound, not a suppressed-
  context turn" (`if workspace and not suppress_local_context:` /
  `_workspace_coding_rules(workspace)`).
- `src/agent_tools/code_graph_tools.py` — `code_graph_index`,
  `code_graph_search` (`semantic: true` routes to `semantic_query`),
  `code_graph_trace`, `code_graph_changes`, `code_graph_impact`,
  `code_graph_architecture`, `code_graph_snippet`.
- `impact` (`src/code_graph/query.py`) — "what else can break and which
  tests to run", from one symbol or (with no symbol) from every symbol the
  current git diff touches (`detect_changes`). BFS over *incoming* `calls`
  edges up to `depth` hops (capped at 6), deduped by symbol id and capped
  at `limit` nodes; each reached node keeps the weakest certainty seen
  along its path and the qualname it was reached through (`via`).
  Unresolved call edges are counted (`unresolved_edges`) but never
  followed. A reached node (or a seed) whose file looks like a test file
  (`tests/`, `test_*.py`, `*_test.py`, `*.test.{ts,tsx,js}`, `*.spec.*`)
  is collected into `affected_tests`/`affected_test_functions`, with a
  ready `python -m pytest -q <files>` `suggested_command` for Python
  tests (capped at 30 files) or a note for JS/TS ones.
- `routes/code_graph_routes.py` — `POST /api/code-graph/index`,
  `GET /api/code-graph/{architecture,search,trace,changes,snippet}`, same
  `require_admin` auth as `routes/tool_registry_routes.py`.
- Registered in `src/tool_schemas.py` (`FUNCTION_TOOL_SCHEMAS`),
  `src/agent_tools/__init__.py` (`TOOL_HANDLERS`/`TOOL_TAGS`),
  `src/tool_capabilities.py` (`READ_WORKSPACE` / `WORKSPACE_UNTRUSTED` —
  same class as `find_symbol`/`callers`/`tests_for`), `src/tool_index.py`
  (`BUILTIN_TOOL_DESCRIPTIONS`), `src/tool_index_examples.py` (`EXAMPLES`,
  4 ES/EN phrases each — `code_graph_impact` included), `src/settings.py` /
  `src/agent_settings_schema.py` (`agent_code_graph_auto_index`), `app.py`
  (`app.include_router(setup_code_graph_routes())`).

## Historical co-change signal

`src/code_graph/cochange.py` — `cochanges(path_or_paths, ...)`: "files that
usually change together with this one", mined from `git log --no-merges
--name-only` (one subprocess call per `(workspace, HEAD sha, max_commits)`,
cached in-process, same subprocess/timeout/cwd-confinement style as
`detect_changes`). A static call/import graph cannot see config, template,
test-fixture or i18n coupling — files nothing calls or imports but that
still need editing together; this is a correlation signal for exactly that
gap, not a dependency. Commits touching more than 50 files (mass renames,
formatter passes) are dropped entirely, and lockfiles/generated paths are
dropped per-file — both would otherwise link every file in the repo to
every other file. Per candidate file: `support` (commits touching both),
`confidence` (support / commits touching the target), a recency-weighted
`score` (exponential decay by commit index, ~100-commit half-life),
`last_seen` (short sha + date) and `exists_now`.

Wired into `impact()` as `historical_cochanges` (default on via
`include_history=True`): the seed file's/files' top co-changes not already
reached by the call-graph BFS, in both the JSON result and a labelled
"Often changed together" section of the text output. `code_graph_impact`'s
tool schema gained `include_history` (boolean, default true). A standalone
`code_graph_cochanges` tool `{path, limit?, max_commits?, min_support?}` is
registered exactly like every other `code_graph_*` tool (schema, handler,
tag, capability, description, ES/EN examples).

## Communities and execution flows (code graph+)

Two questions the graph could trace/impact but not yet answer directly:
"what parts is this repo made of" and "what paths run through it, and how
critical are they".

- `src/code_graph/communities.py` — `communities(root, level=0|1, refresh=,
  summarize=)`, `community(root, id_or_name_or_symbol)`, `community_of(root,
  symbol)`. Builds a weighted, undirected graph over **files** (not
  individual symbols — see the module docstring for why aggregating to file
  granularity is both cheaper and more stable at this scale), weighted by
  edge kind (`calls` > `registers` > `tests` > `imports`) and certainty
  (`exact` > `static_inferred` > `lexical`); an edge whose two endpoints
  disagree on "is this a test file" is dropped from clustering entirely (a
  test file otherwise imports enough of the codebase to smear every real
  community together), though test files still get their own community and
  a community's `test_files` (which tests exercise it) comes from the full,
  unfiltered edge set. Community detection is a from-scratch, deterministic
  two-level Louvain (fixed node order, ties broken by smallest id, a
  standard local-moving phase run once on the file graph for level 0 and
  again on the aggregated level-0 graph for level 1 — literally "communities
  of communities") with a wall-clock/size budget that falls back to
  directory-based grouping (`method: "directory"`) rather than ever hanging
  a turn. Each community carries a stable id (hash of its sorted member
  files), a name (common path prefix + top-fan-in symbol), a deterministic
  `purpose` paragraph, `size` (symbols), `dominant_language`, `cohesion`
  (internal/incident edge weight), `key_symbols`, `routes`, `entry_points`,
  `test_files` and `coupling` (top other communities by cross-edge weight).
  An optional one-sentence model `summary` is added only when
  `code_graph_community_summaries` is on AND the utility model is already
  resident and idle (`src.brain.extract.background_llm_gate`, the same
  check `brain.wiki` uses) — on `summarize=true` or a maintenance pass,
  never inside a chat turn. Persisted in `ce_store` (`code_communities`,
  `code_community_files`), keyed by `(workspace, project_id, fingerprint)`
  where `fingerprint` hashes the index's own `(last_indexed_at, symbols,
  edges)` — an unchanged index is a cache read, a changed one rebuilds.

- `src/code_graph/flows.py` — `flows(root, limit=, sort=, entry=, refresh=)`,
  `flow(root, id_or_entry)`, `affected_flows(symbol="" | from a git diff)`.
  Entry points: HTTP routes, `@tool`/MCP-decorated functions, agent-tool
  `ClassName.execute` methods, `main`-named functions, and public
  functions/methods with outgoing calls but no caller from a non-test file
  (capped and ranked by fan-out — see the module docstring for why CLI
  decorators are not separately detected). From each entry, a cycle-safe,
  depth/node-capped DFS over resolved `calls` edges builds a deterministic
  call tree (`flow`'s indented rendering). Criticality (0..1, weights sum to
  1.0, documented on `_CRITICALITY_WEIGHTS`) combines flow size, files/
  communities spanned, members with high global fan-in, side-effect sinks
  (name matches `db`/`commit`/`write`/`send`/`delete`/`subprocess`/
  `requests`) and the inverse of how much of the flow a test exercises.
  Persisted the same way as communities, sharing the same fingerprint.

- `impact()` gains an additive `affected_flows` key (the execution flows
  through the same seed(s), each with its criticality) — present, possibly
  an empty list, on success; absent (never a false empty) if it could not be
  computed, logged at debug. `change_risk()` gains two additive,
  informational-only fields, `affected_flows_count`/`max_flow_criticality`
  (diff-seeded mode only) — never folded into `factors`/`score`, so every
  existing risk-score assertion is unchanged.

- New built-in MCP server `mcp_servers/code_graph_server.py`, registered in
  `src/builtin_mcp.py` as `"code_graph"` ("Built-in: Code graph"), exposing
  `code_graph_search`/`trace`/`impact`/`architecture`/`communities`/`flows`/
  `affected_flows`/`snippet`/`change_risk` as thin wrappers, same shape as
  `brain_server.py` — not owner-scoped (a code graph has no owner, only a
  workspace), every tool takes `root` explicitly.

- Agent tools `code_graph_communities` {level?, refresh?, summarize?, id?}
  and `code_graph_flows` {entry?, id?, symbol?, base_ref?, limit?} (the
  latter covers list / one flow / affected-by-symbol-or-diff), registered
  everywhere `code_graph_impact` is (schema, capability, tag, description,
  ES/EN examples). Routes `GET /api/code-graph/communities`,
  `/communities/{id}`, `/flows`, `/flows/{id}`, `/affected-flows`, same
  `require_admin` auth as their neighbours. Setting
  `code_graph_community_summaries` (default False) in `src/settings.py` and
  `src/agent_settings_schema.py`'s `code_graph` group.

## Known gap

`context_engine.code_index`'s `EDGE_KINDS` does not include an `INHERITS`
edge — a class's bases are recorded in its `signature` text
(`"class User(Base)"`) but not as a separate graph edge, so
`get_architecture`/`trace_path` cannot walk an inheritance chain today.
Adding that edge kind touches `context_engine/code_index.py`'s extraction
and schema, which is outside R2's file ownership for this wave (shared
Context Engine code) — flagged here rather than changed blind.

## Tests

`tests/test_code_graph.py` — a fixture repo (`tmp_path`, real `git init`)
with three Python modules (a clean `process → load_user → fetch_user` call
chain), one FastAPI route, a class with inheritance, and one JS file with a
`fetch(...)` call. Covers: index build, search (with kind filter), trace
(found + not-found), callers/callees, `detect_changes` against a real
`git diff` (with and without changes), architecture (languages/routes/
hotspots), snippet, incremental reindex (only the touched file is
reparsed), output-size bounds, workspace confinement, the auto-index hook
(no-op outside a running loop; schedules inside one), and full tool/schema/
capability/description/example registration for all six tools.

`tests/test_code_graph_communities.py` — a fixture repo with two clear file
clusters joined by one bridge file: two communities at level 0, deterministic
ids across repeated calls, a cache hit on an unchanged index, a rebuild after
a file changes the fingerprint, the directory fallback under a tiny time
budget, test files excluded from clustering but still reported as coverage,
and `community`/`community_of` resolution by id/name/symbol.

`tests/test_code_graph_flows.py` — a route that calls through three files
into a `commit()`-like sink: one entry point, a deterministic call tree,
cycle safety (a manufactured call cycle never loops), the depth cap, and a
flow that touches a sink ranking above a trivial one-hop flow by
criticality. `affected_flows` from a symbol and from a real `git diff` in a
temp git repo. `impact()` gaining `affected_flows` and every existing
`test_code_graph_*` module still passing.

`tests/test_code_graph_mcp.py` — the `code_graph` built-in MCP server:
importing it starts nothing, `list_tools()` returns a valid schema for
every tool, and each handler runs in-process against a real fixture
workspace (no owner-scoping to test, unlike `brain`/`memory`).
