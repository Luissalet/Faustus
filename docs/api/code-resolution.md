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

## Intake dossier, locate and context pack

Three more read-only code graph surfaces, composed from the existing index,
communities, flows and impact (no second indexer):

- `code_graph_dossier` (`GET /api/code-graph/dossier`, `GET /api/code-graph/dossier/diff`):
  a per-git-revision intake dossier with sections `structure`, `entry_points`,
  `build_run`, `test`, `data`, `integrations`, `screens`, `main_flows` and
  `uncertainties`. Every fact has `evidence` (`path:line`) and a `certainty`
  of `code` (read from source), `config` (read from package.json, pyproject,
  Makefile, Dockerfile, README) or `inferred`. Commands are quoted from the
  file, never composed. A dirty working tree gets its own revision id.
  The dossier is stored in the code graph store (`code_graph_dossiers`);
  `action=diff` (or `/dossier/diff?from_revision=&to_revision=`) lists facts
  added, removed and changed between two stored revisions.
- `code_graph_locate` (`GET /api/code-graph/locate?question=`): candidate
  symbols for "where do I change X" from lexical search per keyword plus the
  semantic lane when an embedder is available, each with the flows and tests
  that touch it. The result is labelled as candidates.
- `code_graph_context_pack` (`POST /api/code-graph/context-pack`, body
  `{symbols, root, budget_lines, base_ref, known_hashes}`): target source,
  contracts (decorators, signature, docstring, pydantic models), direct
  consumers with the exact call lines, tests with their assertion lines and
  flows, ordered by relevance and trimmed to `budget_lines`. What does not fit
  is listed under `left_out` with a `more` handle (tool plus arguments). The
  result's `hashes` ({path: file hash}) passed back as `known_hashes` turns
  unchanged files into "unchanged since <hash>".

All three are available as chat tools and as tools of the `code_graph` MCP server.
Limits: Python is analysed with `ast`, other languages with line patterns;
dynamic imports, programmatic route registration and plugin discovery are
reported as uncertainties, not followed.

## Business route across layers and client handoff

- `code_graph_business_route` (`GET /api/code-graph/business-route?start=&end=`,
  `POST /api/code-graph/business-route` with `{start, end, runtime_trace, root}`):
  follows one user action from a UI element to the write. `start` is the visible
  text or id of a button/link/form, `METHOD /path`, or a symbol; `end` is a free
  phrase (`saved`, `INSERT INTO quotes`, a table or function).
  Result: `hops` (ordered; `layer` ui/frontend/backend/effect/trace, `kind`
  ui_event, handler, http_call, route, call, db_write, db_commit, db_read,
  file_write), `gaps`, `reached_end`, `runtime_trace`, `summary`. Each hop has
  `evidence` (`path:line`) and `certainty`: `resolved` (static match),
  `observed` (confirmed by a `runtime_trace` event), `unresolved` (dynamic URL,
  computed handler or SQL, no matching route). `name-only` matches (similar
  names) appear only in `gaps` and are never followed. Backend hops carry
  `tests` with the path, evidence line and how the test reaches the hop.
  `runtime_trace` events: `{kind: "http"|"call"|"sql", method, path, function,
  file, line, sql?}`; they confirm hops, add hops the static pass missed
  (`from_trace`) and can complete a dynamic client URL (`completed_by_trace`).
- `code_graph_handoff` (`POST /api/code-graph/handoff`, body `{root, out_dir,
  run, timeout}`): plans install, configure, migrate, run, test, recover and
  demo steps from the dossier and, with `run=true`, executes them in a temporary
  copy of the tree (no `.git`, virtualenvs or `node_modules`). Per step:
  `command`, `exit_code`, `duration_s`, `output_tail`, `verdict` (`verified`,
  `failed`, `not run`) and `reason`. Writes `HANDOFF.md` (verified commands as
  instructions; failed and not-run ones under "Not verified") and
  `handoff-report.json` to `out_dir` (default `<root>/.handoff`). It runs the
  repository's own commands and may reach package registries, so it is
  classified as code execution with workspace write and network egress.

Limits: JS handlers are matched by function name inside the project, imports
are not followed across modules, Express sub-routers mounted from other files
are not prefixed, buttons created from JS are not found by text, and Docker is
not executed.
