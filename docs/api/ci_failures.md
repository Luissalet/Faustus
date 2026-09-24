# CI failure analyzer

Reads the actual failure out of the last (or a named) GitHub Actions run
for the repository behind a workspace's `git remote get-url origin` --
which job, which file, which test, the exact error text -- instead of
reporting "the build is red". Read-only, network.

- Module: `src/ci_failures.py`
- Agent tool: `ci_failures` (`src/agent_tools/ci_tools.py`)
- Route: `GET /api/ci/failures` (`routes/ci_failures_routes.py`)

## How it works

1. `repo_from_workspace(workspace)` parses `owner/repo` out of the git
   remote (`git@alias:Owner/Repo.git` or `https://host/Owner/Repo(.git)?`).
2. `list_runs` finds the latest run whose `status=failure` (or a `run_id`
   named directly), `list_jobs` lists its jobs, and `job_log` reads each
   failed job's raw log -- all via the GitHub REST API
   (`src.reach.credentials.get_token("github")` for the token; anonymous
   calls still work, just rate-limited lower). If the REST call fails
   (offline, private repo with no token), each of the three falls back to
   the `gh` CLI when it is on `PATH`, and raises a clear
   `CiFailuresError` naming both failures when neither works.
3. `extract_failures(log_text)` runs a set of heuristics over the cleaned
   log text (ANSI codes and GitHub's per-line timestamps stripped) and
   returns deduplicated `FailureBlock`s: pytest `FAILED`, jest `●`,
   `tsc`/eslint diagnostics, cargo `error[Exxxx]`, go `--- FAIL`, npm
   `ERR!`, `##[error]` annotations, and generic `Error:`/`Traceback`
   blocks.
4. `map_to_workspace` resolves each block's file against the actual
   workspace (refusing anything that would escape it), attaches who last
   touched that file (`git log -1 --format=%an|%ad`), and -- best effort,
   never raising -- the code-graph community that file belongs to, read
   directly from whatever `src.code_graph.communities` already computed
   (never triggers a rebuild here).
5. `analyze(workspace, run_id=None, branch=None)` ties the above together
   into an `Analysis` (`summary_md()` renders a readable report); results
   are cached at `<DATA_DIR>/ci_failures/<owner>/<run_id>.json`, last 30
   runs per owner kept.
6. `propose_fixes(analysis, owner)` asks the configured **utility** model
   (`src.endpoint_resolver.resolve_endpoint("utility", ...)`) for a ranked
   `{file, cause, fix, confidence}` guess per failure. Without a usable
   endpoint/model it returns the mapping alone, each entry noted
   `"no utility model configured"` -- an enrichment's absence never looks
   like the analysis itself failed.

## Agent tool: `ci_failures`

Read-only, network (`ToolEffect.BROKERED_NETWORK_READ` +
`ToolEffect.NETWORK_EGRESS`), plan-mode readable.

Arguments (all optional): `run_id` (int), `branch` (string),
`propose` (bool, default false), `limit` (1-100, default 20).

```json
{"branch": "main", "propose": true}
```

Returns `{"output": "<markdown summary>", "exit_code": 0, "owner", "repo",
"run": {...}, "failure_count", "failures": [...], "from_cache",
"proposed_fixes"?}`. On failure: `{"error": "ci_failures: ...", "exit_code": 1}`.

## Route: `GET /api/ci/failures`

Query params: `workspace` (required), `branch`, `run_id`, `propose`.
Requires an authenticated user (`require_user`) same as
`GET /api/git/radar`. Returns the same shape the tool does, minus
`output`/`exit_code`, plus `summary_md`.

```
curl "http://localhost:8000/api/ci/failures?workspace=/path/to/repo&branch=main"
```
