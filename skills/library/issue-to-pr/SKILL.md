---
name: issue-to-pr
description: Turn a GitHub issue reference into an implemented, tested, pushed pull request using the agent's own git and code-search tools end to end. Use when given an issue URL, an "owner/repo#N" or a bare "#N", and asked to fix it, implement it, or "open a PR for this".
version: 1.0.0
category: engineering
tags: [git, github, pull-request, issue, workflow]
status: published
source: imported
---

## When to Use

The request names or links a specific issue (or a short reference like
`#42`) and asks for it to be fixed, implemented, or turned into a pull
request — not a vague "look at open issues" or a request to review an
already-open PR.

## Procedure

1. **Fetch the issue.** Call `github_issue` with the reference as given (a
   full URL, `owner/repo#N`, or a bare `#N` — a bare number resolves
   against the current workspace's own `origin` remote, so make sure a
   repo workspace is active first). Read its `brief` and any acceptance
   hints pulled from the issue's own checklist before doing anything else —
   they are the actual definition of done, not what the title alone implies.
2. **Understand the codebase before touching it.** Use the project's own
   code-search/code-graph tools (or a plain grep/glob pass when those
   aren't available) to find the code the issue is actually about — the
   relevant module, its tests, and anything that calls into it. Do not
   start editing from the issue title alone.
3. **Plan the change** with whatever planning tool the project offers, or a
   short written plan otherwise, listing the files you expect to touch and
   how you'll verify each acceptance hint from step 1.
4. **Create a branch** for the fix. `github_issue`'s response already
   includes a `suggested_branch` (e.g. `fix/42-short-slug`) — use it unless
   the user asked for a specific name, via the project's branch-creation
   tool (never a raw shell branch command that bypasses the repo's own git
   policy).
5. **Implement the change**, keeping commits small and focused as in
   `git-workflow`. Re-run the acceptance hints from the issue as you go, not
   only at the end.
6. **Run the project's test suite** (or at minimum the tests covering the
   files you touched) before considering the change done. A pull request
   opened against failing tests is worse than no pull request.
7. **Commit** with the project's own commit tool, in the imperative,
   describing the capability or fix — never the issue title verbatim if it
   was written as a complaint rather than a description of the change.
8. **Push** the branch to its remote with the project's own push tool. This
   is a required step before opening the pull request — a PR can only be
   opened for a branch that already has an upstream.
9. **Open the pull request** with the project's own PR-opening tool,
   passing the issue reference so the PR body links back to it (closing the
   issue on merge) and a title/body summarizing what changed, why, and how
   it was tested — a short version of what `pr_body_from_turn`-style
   helpers assemble automatically when the tool supports it.
10. **Report back** with the PR's URL/number, a one-line summary of what
    changed, and which acceptance hints from step 1 are covered — and which,
    if any, were out of scope and left for a follow-up.

## Verification

- The test suite (or at least the tests covering the touched files) passes
  against the final diff, not an earlier one.
- Every acceptance hint pulled from the issue's checklist in step 1 is
  either satisfied or explicitly called out as out of scope in the report.
- `git status` on the branch shows only the files the fix is actually
  about — no stray unrelated changes picked up along the way.
- The opened pull request's body links back to the issue (via the
  `issue_ref` passed to the PR-opening tool) and its title describes the
  fix, not the issue's original complaint verbatim.
- The PR was opened for a branch that was already pushed — never a bare
  local branch a PR tool silently failed to attach to a remote.

## Pitfalls

- Treating the issue title as the whole spec and skipping the body/
  checklist/comments — the real constraints are usually in there, not the
  title.
- Committing everything unrelated the workspace happened to have dirty at
  the time, instead of only the files the fix actually touched.
- Opening the pull request before pushing the branch, or before the tests
  have actually been run once more against the final diff.
- Force-pushing over a branch someone else might already be looking at
  instead of pushing normally and letting a merge conflict surface if one
  exists.
- Writing a pull request title that just repeats the issue title verbatim
  when the issue was phrased as a complaint ("X is broken") rather than a
  description of the fix ("Handle empty input in X").
