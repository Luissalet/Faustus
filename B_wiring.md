# Lot B — wiring for the integrator

One integrator-owned file needs one hook: `app.py` must register the new
router. Nothing else needs a change — `src/bug_hunt.py` and the `bug_hunt`
agent tool are fully wired through the append-only registries already (see
the commit).

## `app.py` — register the bug-hunt router

Next to where `routes/prior_art_routes.py` is registered (same file, same
pattern, near the other Reach-wave routers):

```diff
 from routes.prior_art_routes import setup_prior_art_routes
 app.include_router(setup_prior_art_routes())
+from routes.bug_hunt_routes import setup_bug_hunt_routes
+app.include_router(setup_bug_hunt_routes())
```

`tests/test_b_wiring.py` (marked `xfail(strict=True)`) pins this gap: it
fails until the line above is applied, then flips to an unexpected pass — at
that point delete the `xfail` marker and keep the assertion as a plain
regression check.

## What was NOT touched

- No change to `src/agent_loop.py`, `src/agent_harness.py`,
  `src/tool_execution.py`, `src/agent_tools/coding_tools.py`,
  `routes/chat_helpers.py`, `FAUSTUS.md`, `README.md`, `README.es.md`,
  `OBJETIVOS.md`, `PENDIENTES.md` — none of them needed one.
- The `bug_hunt` agent tool is registered end-to-end through the existing
  append-only registries (`src/tool_schemas.py`, `src/agent_tools/__init__.py`
  — `TOOL_HANDLERS`/`TOOL_TAGS`, `src/tool_capabilities.py`,
  `src/tool_index.py`, `src/tool_index_examples.py`, `src/tool_security.py`
  — `NON_ADMIN_BLOCKED_TOOLS`, NOT `PLAN_MODE_READONLY_TOOLS` since it writes),
  so the agent can call it the moment `src/agent_tools/__init__.py` is
  imported — no `agent_loop.py` change needed for the tool itself (only the
  HTTP route needs the one `app.py` line above).

## How to verify live, once `app.py` is wired

1. Start the app as an admin session (username `admin`).
2. Chat: *"busca bugs en src/settings.py::get_setting"* (or any small,
   pure function in the open workspace) — the agent should call `bug_hunt`,
   report a markdown summary, and — if `keep_tests` was implied/asked — leave
   a `tests/test_bughunt_<slug>.py` file behind for a real bug found.
3. Direct API check:
   ```
   curl -u admin:21052000 -X POST http://localhost:<port>/api/bug-hunt \
     -H 'Content-Type: application/json' \
     -d '{"workspace": "<abs path>", "target": "<file>::<function>"}'
   curl -u admin:21052000 'http://localhost:<port>/api/bug-hunt/reports'
   ```
4. Confirm `<workspace>/.faustus/bughunt/` is empty afterwards (scratch files
   are deleted unless `keep_scratch` is requested) and that
   `<DATA_DIR>/bug_hunt/<owner>/` has the JSON report.

## What could NOT be verified from this worktree

- The live chat phrase end-to-end through the real agent loop and a real
  local model (Ollama/qwen) — this worktree has no running Faustus instance
  or model endpoint attached; `src/bug_hunt.py`'s own test suite
  (`tests/test_bug_hunt.py`) exercises the full pipeline with the LLM calls
  monkeypatched instead, plus a real, unmonkeypatched pytest subprocess for
  `run_suite`.
- The `/api/bug-hunt` route's admin gate was smoke-tested with `AUTH_ENABLED`
  disabled (same pattern `tests/test_tool_arg_policy.py` uses); the real
  `admin`/`21052000` login path was not exercised from this worktree.
