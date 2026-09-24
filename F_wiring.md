# F_wiring.md — code_history (git history understanding, Lot F)

This lot's own files (`src/code_history.py`, `src/agent_tools/code_history_tools.py`,
`routes/code_history_routes.py`) are fully tested in `tests/test_code_history.py`.
Everything below touches integrator-owned files (`src/agent_loop.py`, `app.py`)
and is NOT applied here — this file is the diff for the integrator to apply and
un-xfail `tests/test_f_wiring.py`.

Registry files already appended (append-only, own block at the end of each
structure): `src/tool_schemas.py` (`code_history` function schema),
`src/agent_tools/__init__.py` (import + `TOOL_HANDLERS["code_history"]` +
`TOOL_TAGS`), `src/tool_capabilities.py` (`READ_WORKSPACE` /
`WORKSPACE_UNTRUSTED`), `src/tool_index.py` (description),
`src/tool_index_examples.py` (EN/ES examples), `src/tool_security.py`
(`PLAN_MODE_READONLY_TOOLS` — read-only inspection, not admin-only, same class
as `find_symbol`/`callers`/`tests_for`).

## 1. `app.py` — register the router

```diff
+from routes.code_history_routes import setup_code_history_routes
```
next to the other `routes.*_routes` imports, and
```diff
+app.include_router(setup_code_history_routes())
```
next to `app.include_router(setup_code_graph_routes())`.

## 2. `src/agent_loop.py` — the git-read family (`_git_read`)

`code_history` answers a git-history question exactly like `git_log`/`git_diff`
do, so it belongs in the same read-only floor the git-context heuristic already
grants a workspace that is (or contains) a git repository (~line 7923):

```diff
             from src.tool_execution import _GIT_TOOL_NAMES
-            _git_read = {"git_radar", "git_status", "git_log", "git_diff"}
+            _git_read = {"git_radar", "git_status", "git_log", "git_diff", "code_history"}
```

`code_history` is not itself in `_GIT_TOOL_NAMES` (`src/tool_execution.py`,
integrator-owned) — it does not need the repo-resolution/policy machinery
those tools share, since it never mutates anything and only ever reads a
`path` already confined to the active workspace. Leaving it out of
`_GIT_TOOL_NAMES` and only adding it to `_git_read`'s *offered* set means the
`_git_selected_action` check (`set(_relevant_tools) & (set(_GIT_TOOL_NAMES) -
_git_read)`) is unaffected — a turn that already picked a mutating git action
still gets the full git family, and one that only reads history still gets
`code_history` alongside `git_log`/`git_diff` when a repo is present.

## 3. `src/agent_loop.py` — the code-intel family (`_CODE_INTEL_FAMILY`)

"Is this file risky to touch", "who else touches this", "what usually breaks
with this" are the same class of workspace-structure question the code-intel
intent regex already recognises (~line 636) for `code_graph_*`/`tests_for`.
Suggested addition — a `code_history` mention (blame, risk, co-change,
history) should also route into that family when the intent regex fires:

```diff
 _CODE_INTEL_FAMILY = frozenset({
     "code_graph_impact", "code_graph_flows", "code_graph_communities", "tests_for",
-    "code_graph_drift",
+    "code_graph_drift", "code_history",
 })
```

No change to `_CODE_INTEL_INTENT_RE` is required — its existing "impact"/
"affected"/"what breaks" wording already covers "is this risky", "who touched
this" reads more naturally as its own explicit example set (already added to
`src/tool_index_examples.py`), matched by the tool-RAG retrieval index rather
than this regex.

## How to verify live

Once wired:
- `curl -s "http://localhost:<port>/api/code-history?path=src/code_history.py&mode=risk"`
  (admin session) returns a `score` + `explanation`.
- A chat turn on a git-backed workspace asking "who has changed this file the
  most" should see `code_history` among its offered tools without asking for
  git explicitly (`_git_read` addition), and the same chat asking "is this
  file risky to touch" should get the code-intel family including
  `code_history` (`_CODE_INTEL_FAMILY` addition).

## What could NOT be verified from this worktree

Both `src/agent_loop.py` changes and the `app.py` route registration — those
files are integrator-owned and off limits to this lot; `tests/test_f_wiring.py`
(`xfail(strict=True)`) encodes the expected diffs and will start failing,
loudly, the moment they land — the signal to delete the `xfail` mark.
