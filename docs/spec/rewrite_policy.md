# H4 — rewrite policy (`src/rewrite_policy.py`)

## Problem

The silhouettes forensic analysis (chat `b781a317`, 14-09) shows the same
file (`silhouettes/vector.py`, `trace.py` — both well over 150 lines)
rewritten whole with `write_file` 8-10 times inside a single turn. The
harness already **notices** this (`agent_harness.py::TurnLedger.record`
appends a `whole_file_rewrite:<path>` note), but nothing discourages it: a
27B local model that does not trust its own last edit "starts over" from
memory every time it sees a new failure, and each full rewrite is a chance
to silently drop code from a part of the file it is not currently looking
at (the concrete bug in that chat: `contour` treated as a list in one
rewrite and as an `ndarray` in the next).

## Policy

One `RewritePolicy` instance per **turn** (never shared across turns, and
never between a coordinator and a worker it delegated to — same contract as
`loop_breaker.LoopPolicy`).

`observe(path, tool, lines_before, lines_after=0) -> "ok" | "require_edit" | "block"`

- Only `tool == "write_file"` can ever trigger it. `edit_file` and
  `apply_patch` never count, and a surgical edit between two whole-file
  rewrites does not reset or excuse the next one.
- `lines_before == 0` (the file did not exist before this write) → always
  `"ok"`. A new file is never a rewrite of anything.
- `lines_before <= min_lines` (default 150) → always `"ok"`. The policy only
  protects files too large for a small model to hold in working memory
  across several full rewrites.
- Otherwise the call is a **qualifying rewrite** of that path this turn. The
  Nth qualifying rewrite of the same path:
  - `1..require_edit_after-1` (default: 1st) → `"ok"`
  - `require_edit_after..block_after-1` (default: 2nd–3rd) → `"require_edit"`
  - `>= block_after` (default: 4th+) → `"block"`

`RewritePolicy.from_settings()` reads `agent_rewrite_policy` ("off" disables
the whole policy; any other value, default `"require_edit"`, enables it),
`agent_rewrite_policy_require_edit_after` (default 2),
`agent_rewrite_policy_block_after` (default 4) and
`agent_rewrite_policy_min_lines` (default 150).

`deny_message(path, verdict, count, min_lines)` / `deny_result(...)` build
the tool-error text and payload an integrator's `write_file` returns for a
`require_edit`/`block` verdict — see `H45_wiring.md`.

## Non-goals

- Does not touch `edit_file`/`apply_patch` behavior at all.
- Does not persist anything — the count resets every turn by construction
  (a fresh instance), matching CONTRATO.md's "reset por turno".
- Does not decide *how* the harness escalates on `"block"` (e.g. injecting
  "read the file and the diff before touching it again") — that is the
  integrator's turn-loop concern, described in `H45_wiring.md`.
