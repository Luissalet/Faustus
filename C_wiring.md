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
