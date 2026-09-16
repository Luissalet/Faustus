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
  `code_graph_trace`, `code_graph_changes`, `code_graph_architecture`,
  `code_graph_snippet`.
- `routes/code_graph_routes.py` — `POST /api/code-graph/index`,
  `GET /api/code-graph/{architecture,search,trace,changes,snippet}`, same
  `require_admin` auth as `routes/tool_registry_routes.py`.
- Registered in `src/tool_schemas.py` (`FUNCTION_TOOL_SCHEMAS`),
  `src/agent_tools/__init__.py` (`TOOL_HANDLERS`/`TOOL_TAGS`),
  `src/tool_capabilities.py` (`READ_WORKSPACE` / `WORKSPACE_UNTRUSTED` —
  same class as `find_symbol`/`callers`/`tests_for`), `src/tool_index.py`
  (`BUILTIN_TOOL_DESCRIPTIONS`), `src/tool_index_examples.py` (`EXAMPLES`,
  4 ES/EN phrases each), `src/settings.py` / `src/agent_settings_schema.py`
  (`agent_code_graph_auto_index`), `app.py`
  (`app.include_router(setup_code_graph_routes())`).

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
