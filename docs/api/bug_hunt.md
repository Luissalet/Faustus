# Bug hunt: an autonomous bug hunter

Given a function, a class, a file, or a whole directory, `bug_hunt`
understands the code, generates edge-case tests for it, runs them isolated,
and separates a real bug in the code from a wrong assumption in the
generated test — instead of just handing back a wall of failing tests.

Implementation: `src/bug_hunt.py`. Agent tool: `bug_hunt`
(`src/agent_tools/bug_hunt_tools.py`). HTTP routes:
`routes/bug_hunt_routes.py`.

## Pipeline

1. **`plan_targets(workspace, target)`** — understands the code. `target` is
   a file path, `path::symbol` (one function/class), or a directory. Python
   files are walked with `ast` to enumerate top-level functions, classes and
   their methods (private names skipped unless asked for directly); other
   languages get one file-level target. Each `Target` carries its source
   snippet (≤ 4 KB), signature, docstring, and up to 5 known callers — pulled
   from `src.code_graph` when the symbol resolves there, a plain grep
   otherwise.

2. **`generate_tests(target, *, owner, model=None, max_cases=12)`** — asks
   the resolved *utility* model (small, background-tier — the same class of
   call `src.instincts.extract_from_turn` makes) for ONE pytest file: normal
   cases, boundary cases (empty, `None`, zero, negative, huge, unicode),
   invalid-input handling, and idempotency/ordering when relevant. Every
   generated test must state its expected behaviour in a comment, and any
   test whose expectation is a guess rather than a stated contract must have
   `assumption` in its function name. The model's output is validated:
   `ast.parse` must succeed, and an AST scan rejects any test that imports a
   networking module (`requests`, `httpx`, sockets, …) or calls something
   destructive (`os.remove`, `shutil.rmtree`, `subprocess.*`, …). When no
   model is reachable, or its output fails validation, a deterministic
   template suite is produced instead (a smoke test that the target is
   callable, plus one test per parameter calling it with that parameter set
   to `None`) — the tool always returns something runnable.

3. **`run_suite(workspace, suite, timeout_s=120)`** — writes the suite to
   `<workspace>/.faustus/bughunt/test_bh_<slug>_<hash>.py` (never inside the
   project's own `tests/`), runs
   `python -m pytest -v -p no:cacheprovider <file>` with the workspace's own
   interpreter (venv if one exists) and the workspace on `PYTHONPATH`, and
   parses per-test pass/fail with the failing tracebacks.

4. **`triage(target, run_result, *, owner, model=None)`** — for every failing
   test, decides `bug` / `test_wrong` / `unclear` with `root_cause`,
   `fix_suggestion` and a `low`/`medium`/`high` severity. A model call is
   preferred (one batched call for all failing tests of a target); without a
   reachable model, a deterministic heuristic applies: an `AssertionError` in
   a test whose name contains `assumption` → `unclear`; a `TypeError` /
   `AttributeError` on a `None` input with no guard → `bug` (medium); a
   `ZeroDivisionError`, `IndexError` or `KeyError` on an unguarded
   empty/short input → `bug` (medium); anything else → `unclear`.

5. **`hunt(workspace, target, *, owner, keep_tests=False, ...)`** —
   orchestrates the four steps above across up to 6 targets per call, and
   returns a `Report` (`to_markdown()` / `to_dict()`). With `keep_tests=True`,
   the exact test functions whose verdict is `bug` are copied into
   `<workspace>/tests/test_bughunt_<slug>.py` as permanent regression tests
   (appended, deduped by test function name across repeated hunts). The
   scratch run file is always deleted after triage unless `keep_scratch=True`.
   Every report is persisted to
   `<DATA_DIR>/bug_hunt/<owner>/<unix_ts>_<slug>.json`, keeping the last 50
   per owner.

## Settings

- `bug_hunt_model` (default `""` = the resolved utility endpoint's model)
- `bug_hunt_max_cases` (default `12`)
- `bug_hunt_timeout_seconds` (default `120`)

## Agent tool

`bug_hunt {target, workspace?, max_cases?, keep_tests?, run_only?}` — writes
under the workspace (`.faustus/bughunt/`, and `tests/` with `keep_tests`), so
it carries the same `write_file`-class effect and approval policy, never
read-only. `run_only=true` generates and runs the tests but skips triage and
keeping them, for a quick "does it already break" pass.

## HTTP routes (admin-only)

- `POST /api/bug-hunt` `{workspace, target, keep_tests?, max_cases?,
  timeout_s?, model?}` — runs synchronously (bounded by `timeout_s`) and
  returns the full `Report`.
- `GET /api/bug-hunt/reports?workspace=` — the calling owner's saved reports,
  newest first, optionally filtered to one workspace.

## Example

```
POST /api/bug-hunt
{"workspace": "/path/to/project", "target": "src/pricing.py::apply_discount"}
```

Chat phrase: *"busca bugs en src/pricing.py"* / *"find edge-case bugs in
apply_discount"*.
