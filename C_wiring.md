# Lot C — wiring for the integrator

Two integrator-owned files need one hook each. Neither is touched by this
lot; the exact diffs are below, plus the matching xfail test in
`tests/test_c_wiring.py`.

## 1. `src/agent_loop.py` — inject the project-rules block

Right after the existing `project_instructions.block(...)` call (around line
4399, inside the same `try:` that builds `agent_prompt`), with the SAME
`trusted` value it already computed:

```diff
         try:
             from src import project_instructions as _pinstr
             _instr_trusted = True
             try:
                 from src import workspace_trust as _wtrust
                 _instr_trusted = _wtrust.instructions_trusted(workspace)
             except Exception as _wt_err:      # pragma: no cover - import-time only
                 logger.debug("[instructions] trust check unavailable: %s", _wt_err)
                 _instr_trusted = True
             agent_prompt += _pinstr.block(workspace, trusted=_instr_trusted)
         except Exception as _pi_err:
             logger.debug("[instructions] injection failed: %s", _pi_err)
+        try:
+            from src import project_rules as _prules
+            _prules_langs = _prules.languages_for(workspace)
+            agent_prompt += _prules.block(workspace, trusted=_instr_trusted, languages=_prules_langs)
+        except Exception as _pr_err:
+            logger.debug("[project_rules] injection failed: %s", _pr_err)
         try:
             agent_prompt += _big_task_strategy_block()
         except Exception:
             pass
```

Notes for whoever wires this:
- `_instr_trusted` is already in scope from the block right above — reused,
  not recomputed, so both blocks agree on whether this folder is approved.
- `project_rules.block` never raises and returns `""` when the feature is
  off or there is nothing to say, same discipline as `project_instructions.
  block`.

## 2. `app.py` — register the two new routers

Next to where `routes/skills_routes.py` and `routes/tool_arg_policy_routes.py`
are already registered (same file, same pattern):

```diff
 from routes.skills_routes import setup_skills_routes
 app.include_router(setup_skills_routes(skills_manager))
+from routes.skill_library_routes import setup_skill_library_routes
+app.include_router(setup_skill_library_routes())
```

```diff
 from routes.tool_arg_policy_routes import setup_tool_arg_policy_routes
 app.include_router(setup_tool_arg_policy_routes())
+from routes.project_rules_routes import setup_project_rules_routes
+app.include_router(setup_project_rules_routes())
```

Either location works (both routers are self-contained, no shared state with
neighbours); grouping them next to the routes they most resemble is just for
readability.

## Test

`tests/test_c_wiring.py` asserts, `xfail(strict=True)`, that:
- `src.agent_loop`'s system prompt for a workspace with a `.faustus/rules/`
  file contains that file's rule text (fails today because the hook above
  isn't wired yet; un-xfail once it is).
- `app` (the FastAPI instance) has a route for `GET /api/rules/library` and
  `GET /api/skills/library` (fails today for the same reason).

**Status: DONE** — both hooks above are already applied in this worktree's
`app.py`/`agent_loop.py` (a prior pass of this lot); the two tests above now
pass as plain regression checks (their `xfail` markers were removed once
they started passing, per this file's own instruction).

---

## 3. `app.py` — register the CI failures router (new in this pass)

Same pattern, next to the other `/api/*` routers already registered:

```diff
 from routes.git_routes import setup_git_routes
 app.include_router(setup_git_routes())
+from routes.ci_failures_routes import setup_ci_failures_routes
+app.include_router(setup_ci_failures_routes())
```

Any location alongside the other route registrations works — the router is
self-contained (`src/ci_failures.py`, no shared state with neighbours).

### Verify live once wired

```
curl "http://localhost:8000/api/ci/failures?workspace=/absolute/path/to/a/repo&branch=main"
```

Expected: `{"owner", "repo", "run", "failures": [...], "summary_md", ...}`
for a repo with a failed GitHub Actions run reachable either through a
configured `reach_github_token` setting or a locally authenticated `gh`
CLI; `{"error": "..."}` with a plain-English reason otherwise (no remote,
no failed run, no token and no `gh`).

In chat: *"why did CI fail on this repo"*, *"read the actual test failure
from the last GitHub Actions run"*, *"check run 12345 and tell me what
broke"* should route to the `ci_failures` tool.

### What could NOT be verified from this worktree

- No live GitHub repository/run was reachable from here to exercise the
  real REST call or the `gh` CLI fallback end to end — `tests/
  test_ci_failures.py` covers both paths with `httpx.MockTransport` and a
  monkeypatched `subprocess.run` instead (contract rule: no network in
  tests). The integrator or a follow-up manual pass should hit the route
  above against one real repo with a failed run once wired.
- `propose_fixes` was exercised only against the "no utility model
  configured" fallback path and a monkeypatched `llm_call_async`; it was
  not run against a live local model.
- The deployment review team's `deploy-lead` coordinator was verified by
  parsing (`src.agent_defs.parse`/`resolve_tasks`) and by manual reading of
  its instructions, not by an actual live `delegate_agents` run — that
  needs a running agent loop with worker capacity, out of scope for a
  library-file-only worktree.

### `tests/test_c_wiring.py` addition

One more `xfail(strict=True)` test asserts `app` has a route for
`GET /api/ci/failures` — fails today, flips green (and should have its
marker removed, same as the two above) once the diff above is applied.
