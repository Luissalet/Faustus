# Code history — `src/code_history.py`, `GET /api/code-history`, tool `code_history`

Git history understanding for the codebase explorer: for a file, or one
symbol inside it, who changed it, how often, with which commits, what else
usually changes alongside it, and the risk that implies. Everything runs
through the `git` binary via `subprocess`, confined to the caller's
workspace and bounded by a timeout; nothing raises past the module's own
boundary — a broken repo, a missing `git`, or a symbol that no longer exists
all come back as `{"error": ...}`.

## Functions (`src/code_history.py`)

- `file_history(workspace, path, *, limit=30)` — commits touching `path`
  newest-first (`sha`, `date`, `author`, `subject`), per-commit churn
  (`adds`/`dels` via `--numstat`), `first_touched`/`last_touched`, and an
  authors ranking.
- `symbol_history(workspace, path, symbol, *, limit=20)` — commits that
  touched `symbol`'s definition. Uses `git log -L :<symbol>:<path>` when the
  host's git understands that syntax; otherwise finds the symbol's current
  line range (`ast` for Python, a regex for `function`/`class`/`const x =`
  otherwise) and falls back to `git log -L start,end:path`. Returns the
  matched commits plus a diff excerpt for the latest one, clipped to 60
  lines.
- `blame_summary(workspace, path, *, start=None, end=None)` — `git blame
  --line-porcelain`, aggregated into `{author, lines, pct}`, the
  oldest/newest surviving commit, and the "hot" commits responsible for the
  most lines currently in the file.
- `co_change(workspace, path, *, limit=200)` — over `path`'s last `limit`
  commits, the files that most often appear in the same commit, with counts
  and a ratio (co-occurrences / commits touching `path`) — "changing this
  usually also touches …".
- `risk(workspace, path)` — a 0–1 heuristic score from churn in the last 90
  days, number of distinct authors, co-change fan-out, and whether a related
  test file exists (`src.project_tests.related_test_files` when
  importable), plus a short human-readable explanation.
- `explain(workspace, path, symbol=None)` — combines all of the above and
  adds `summary_md`, a short Markdown answer. Cached in memory per
  `(workspace, HEAD sha, path, symbol)`, up to 200 entries (LRU eviction);
  the cache is naturally invalidated the moment `HEAD` moves.

## Tool: `code_history`

Read-only, workspace-confined the same way `grep`/`glob`/`find_symbol` are.

```json
{"path": "src/app.py", "symbol": "handle_request", "mode": "explain", "limit": 30}
```

- `path` (required) — file path, relative to the workspace or absolute
  inside it.
- `symbol` (optional) — narrows to one symbol's history (`mode=symbol`
  requires it).
- `mode` — `explain` (default, everything combined), `file`, `symbol`,
  `blame`, `co_change`, or `risk`.
- `limit` — commits to consider (mode-dependent default).
- `start`/`end` — for `mode=blame`, a 1-based line range.
- `workspace` — optional override; defaults to the turn's active workspace.

## Route

```
GET /api/code-history?workspace=&path=&symbol=&mode=&limit=&start=&end=
```

Admin-only (`require_admin`), same pattern as `routes/code_graph_routes.py`.
`workspace` is confined the same way `code_history`'s own callers confine it;
an out-of-workspace path is a 400, not a 500. Returns the same JSON shape
the matching `src.code_history` function returns; an `{"error": ...}` result
is surfaced as HTTP 400.

## Notes

- Every function is best-effort: a non-repo workspace, a missing `git`, a
  timed-out subprocess, or a symbol that no longer exists all come back as
  `{"error": ...}` — none of them raise.
- `risk`'s score is a heuristic meant to prioritize attention, not a formal
  metric: high churn + many authors + a wide co-change fan-out + no related
  test all push it up; the explanation always names which factors did.
