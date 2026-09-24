# Fix memory (Lot D)

The agent grows smarter with every solved issue. After a turn that changed
files and ran (or attempted) tests, one compact record is appended to this
project's own fix log — the request, the files/functions touched, the error
signatures seen, the tests run and their outcome, and a short solution
summary. Before a similar coding task, past fixes can be recalled and
injected as a short "Past fixes in this project" block, or looked up
directly with the `recall_fixes` tool.

Never a gate: recording and recall only ever add data. No approval is
required for either.

## Storage

`<DATA_DIR>/fix_memory/<owner-safe>/<project-safe>.jsonl` — one JSON entry
per line, appended (never rewritten in place except for rotation/forget). A
sibling `<project-safe>.index.json` keeps `{id: [keywords...]}`.

Entry shape:

```json
{
  "id": "fix-1a2b3c4d5e6f7089",
  "ts": 1732450000.0,
  "project": "/home/user/my-app",
  "task": "fix the login timeout bug",
  "files": ["src/auth.py"],
  "symbols": ["authenticate"],
  "errors": ["ValueError: timeout after # seconds"],
  "tests": {"command": "pytest -q tests/test_auth.py", "ok": true, "summary": "3 passed"},
  "outcome": "fixed",
  "solution": "changed auth.py; tests: 3 passed",
  "tags": ["py", "fixed"]
}
```

`outcome` is one of `fixed` (tests ran and passed), `partial` (tests ran and
failed), or `unverified` (no tests ran, or the harness's own completion gate
downgraded the turn). Rotation keeps at most 500 entries per project,
dropping `unverified` entries first (oldest first), then the oldest
remaining entries.

## Python API (`src/fix_memory.py`)

- `record_from_turn(owner, *, project_key, user_message, tool_events, harness_meta=None, round_texts=None, workspace="") -> entry|None` —
  called after a turn. Returns `None` (and writes nothing) when the feature
  is off, there is no owner, or no file was actually touched.
- `recall(owner, project_key, query, *, files=None, errors=None, k=5) -> list[entry]` —
  lexical (Jaccard) overlap of `query` against each entry's
  task+solution+errors, boosted by shared `files` and matching error
  signatures, recency tie-break.
- `prompt_block(entries, budget_tokens=600) -> str` — one line per entry,
  most relevant first, cut to the token budget.
- `stats(owner, project_key)` — counts by outcome, top tags.
- `forget(owner, id)` — delete one entry (scans every project this owner
  has, since an id is a unique content hash).

## Agent tool: `recall_fixes`

Read-only, no side effects. Args: `{query, files?, error?, k?}`. Looks up
past fixes in the current project (`ctx.workspace`/`ctx.project_id`).

## Routes (`routes/fix_memory_routes.py`)

```
GET    /api/fix-memory?workspace=&project_id=&q=&k=
GET    /api/fix-memory/stats?workspace=&project_id=
DELETE /api/fix-memory/{id}
```

Owner-scoped, not admin-only.

## Settings

- `fix_memory_enabled` (default `True`) — master switch.
- `fix_memory_prompt_budget_tokens` (default `600`) — budget for the
  auto-injected prompt block.
- `fix_memory_auto_recall` (default `True`) — whether a coding-task turn
  automatically recalls and injects past fixes, vs. only ever being
  reachable through `recall_fixes`.
