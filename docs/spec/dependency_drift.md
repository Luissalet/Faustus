# H2 — dependency drift

`src/dependency_drift.py`. Compensates for what silhouettes_analysis.md chats
#3, #9 and #21 show a local 27B model doing badly: discovering a missing
package (`shapely`, later `jsonschema`) only after running code, reading the
traceback, and diagnosing it — burning whole rounds on a check that
`importlib.metadata` answers deterministically.

## What it does

1. Reads what the project **declares**:
   - every `requirements*.txt` at the project root (`packaging.Requirement`
     when available, else a minimal parser that still understands
     `; sys_platform == "win32"`-style markers);
   - `pyproject.toml`'s `[project].dependencies` (PEP 621, via `tomllib`);
   - `package.json`'s `dependencies` + `devDependencies`.
2. Checks it against what is **actually installed**:
   - Python: the project's own interpreter
     (`src.agent_tools.subprocess_tools.project_python`) runs a tiny script
     out-of-process (`subprocess.run`, argv list, `shell=False`, 20 s
     timeout) that calls `importlib.metadata.distribution(name)` per
     declared name;
   - Node: `node_modules/<pkg>/package.json` existence (scoped packages use
     `node_modules/@scope/name`).
3. Caches the result at `DATA_DIR/dependency_drift/<workspace_hash>.json`,
   keyed by `(sha1 of every deps file's content, mtime of site-packages /
   node_modules)` — any edit to a deps file, or any install/uninstall,
   invalidates it.

## Public API

- `check_drift(project_root, *, python_exe=None, env=None, use_cache=True) -> DriftReport`
  — the one entry point. `DriftReport` is `{missing_python, missing_node,
  venv, hint, from_cache, ok}`.
- `system_note(report, language="en") -> str` — ES/EN text for a system
  message ("faltan X — instálalas con install_dependencies antes de ejecutar
  nada" / the English equivalent). Empty string when `report.ok`.
- `auto_install(report, project_root, *, owner="", enabled=None) ->
  AutoInstallResult` — optional, gated by the `agent_auto_install_missing_deps`
  setting (default `False`). Goes through `src.tool_execution`'s **EXEC-05**
  `plan_dependency_install` / `execute_dependency_install` flow exclusively —
  this module never spawns `pip`/`npm` itself.
- `clear_cache(project_root)` — manual/test invalidation hook.

## What it deliberately does not do

- It does not run anything the model asked for; it only inspects declared
  vs. installed state.
- It does not call `pip install`/`npm install` directly under any
  circumstance — `auto_install` is a thin wrapper around the already-audited
  EXEC-05 plan/approve/execute path (project-scoped installs only, package
  names validated, plan hashed so a repeat approval is not re-asked).
- A subprocess failure (bad interpreter, timeout, malformed output) fails
  **open** toward "could not verify, treat as missing" rather than silently
  reporting everything present.

## Settings (new)

- `agent_auto_install_missing_deps` (bool, default `False`) — see
  `H2_wiring.md` for where this is read and how the note gets injected.

## Tests

`tests/test_dependency_drift.py` — 24 tests, all real: requirements/pyproject/
package.json parsing, Windows-only markers on a non-Windows host, a real
subprocess check against synthetic `.dist-info` records on `PYTHONPATH` (real
`importlib.metadata`, not mocked), cache hit/invalidation, and an
`auto_install` test that captures the exact argv EXEC-05 built to prove no
direct `pip`/`npm` call ever happens.
