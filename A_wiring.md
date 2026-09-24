# Lot A -- wiring notes

"Give the agent a GitHub issue and it ends with a pull request": `src/github_pr.py`
(deterministic helpers, no tool/agent dependency) plus two agent tools in
`src/agent_tools/git_tools.py` -- `github_issue` (read, network) and
`git_open_pr` (side effect, network+external).

Both tools are already fully registered and functional (schemas, handlers,
tags, capabilities, descriptions, examples, security lists -- see the diffs
in the commit). Nothing below blocks the agent by default; it only closes
two gaps in integrator-owned files that make the git tool family and the
approval path fully coherent for these two tools too.

## 1. `src/tool_execution.py` -- `_GIT_TOOL_NAMES`

Today: dispatched through `elif tool in dynamic_handlers:`, which already
passes `owner` but not `human_approved` (defaults to `False`). This means an
already-sealed approval card from earlier in the same turn does not
auto-cover a `git_open_pr` call the way it does for `git_push` -- the model
still has the always-available fallback (ask the user, then retry with
`"user_confirmed": true"`), so nothing is blocked, it is just one hop
slower.

```diff
--- a/src/tool_execution.py
+++ b/src/tool_execution.py
@@ -64,7 +64,9 @@
 _GIT_TOOL_NAMES = frozenset({
     "git_init", "git_publish", "git_radar", "git_status", "git_log", "git_diff",
     "git_branch", "git_checkout", "git_commit",
     "git_merge", "git_delete_branch",  # Lote 89
     "git_push", "git_pull", "git_fetch",
+    "github_issue", "git_open_pr",
 })
```

## 2. `src/agent_loop.py` -- git tool floor (`_git_read`)

Today: the "git tools offered" floor (search `_git_read = {"git_radar", ...}`
around line 7924) only ever adds tools from `_GIT_TOOL_NAMES`, so until (1)
above lands, `github_issue`/`git_open_pr` are never auto-added to a turn just
because it mentions "issue"/"pull request" -- tool-RAG retrieval (the
EXAMPLES phrases already added in `src/tool_index_examples.py`) can still
surface them on their own semantic merit. Once (1) lands, the two tools ride
along automatically; `github_issue` belongs in the *read* half of the git
family (same class as `git_status`/`git_log`/`git_diff` -- it never mutates
anything), `git_open_pr` belongs in the *write* half (same class as
`git_push`/`git_commit`), so only `_git_read` needs a line changed:

```diff
--- a/src/agent_loop.py
+++ b/src/agent_loop.py
@@ -7921,7 +7921,7 @@ async def _stream_agent_loop_body(
             from src.tool_execution import _GIT_TOOL_NAMES
-            _git_read = {"git_radar", "git_status", "git_log", "git_diff"}
+            _git_read = {"git_radar", "git_status", "git_log", "git_diff", "github_issue"}
```

## How to verify live once wired

```
curl -s -X POST http://localhost:<port>/api/chat -H 'Content-Type: application/json' \
  -d '{"message": "read GitHub issue acme/widgets#42 and give me a brief", ...}'
```
should call `github_issue` and return its `brief`/`suggested_branch`.

```
curl -s -X POST http://localhost:<port>/api/chat -H 'Content-Type: application/json' \
  -d '{"message": "open a pull request for this branch, closes #42", ...}'
```
against a workspace whose current branch is already pushed should call
`git_open_pr` and, once (1) lands, ride an existing approval card instead of
needing a second `user_confirmed: true` round trip.

## What could NOT be verified in this worktree

* No live GitHub token/`gh` login is configured here, so `fetch_issue`/
  `open_pull_request` were verified against `httpx.MockTransport` and faked
  `gh`/`git` subprocess calls only (see `tests/test_github_pr.py`), never
  against the real GitHub API.
* The chat-level curl checks above (tool actually gets offered end to end
  through `agent_loop.py`'s retrieval + floor, then dispatched and rendered
  in the transcript) need a running server with a real workspace and are
  left for the integrator to run after wiring (1)/(2) in.
