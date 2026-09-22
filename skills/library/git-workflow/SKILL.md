---
name: git-workflow
description: Branching, commit message, and merge-vs-rebase conventions that keep history readable and bisectable. Use when creating a branch, writing a commit message, or choosing how to combine two lines of history.
version: 1.0.0
category: engineering
tags: [git, version-control, workflow]
status: published
source: imported
---

## When to Use

Every commit and branch, as a default — and specifically whenever the
project's own convention isn't obvious from `git log` yet (a brand-new
repo, or your first commit in one you just onboarded to).

## Procedure

1. **Pick a branching model that matches the project's release cadence.**
   Trunk-based (short-lived branches, frequent merges to main) suits a
   continuously-deployed service; a longer-lived release branch suits
   something versioned and shipped in batches. Match what the project
   already does rather than introducing a second model.
2. **Write commit messages in the imperative, one logical change per
   commit**: "Add retry to the webhook sender", not "Added retry" or "fixes
   stuff". If the project uses a structured format (`type(scope): subject`),
   follow it exactly, including its allowed types.
3. **Explain why in the body, not what** — the diff already shows what
   changed; the body earns its space by saying why this approach, what
   alternative was rejected, or what it fixes and how you know.
4. **Keep commits small and reviewable.** A commit that mixes a rename, a
   behaviour change, and a formatting pass is unreviewable and unbisectable
   — split them, even if it means more commits for one logical task.
5. **Choose merge vs. rebase deliberately.** Rebase a feature branch onto
   the latest main before opening/updating a PR to keep history linear and
   the diff small; merge (not rebase) once something is already shared with
   others, since rebasing published history rewrites commits other people
   have based work on.
6. **Never force-push a shared branch** without the owning team's explicit
   go-ahead — it silently discards commits others may have already pulled.
7. **Tag or checkpoint before a risky operation** (a large refactor, a
   rebase across many commits) so there's a known-good point to return to
   without needing to reconstruct it from memory.

## Pitfalls

- A commit message that describes the mechanics ("update file.py") instead
  of the behaviour ("fix off-by-one in page size calculation") — six months
  later, nobody can tell from `git log` which commit fixed which bug.
- Squashing an entire multi-day feature into one commit, losing the ability
  to bisect a regression down to the specific change that introduced it.
- Rebasing a branch that other people have already pulled and built on top
  of, forcing everyone downstream to deal with a rewritten history.
- Committing generated files, build artifacts, or credentials because
  `.gitignore` wasn't checked first.
- Mixing an unrelated drive-by fix into a commit about something else,
  making the diff harder to review and impossible to revert independently.

## Verification

- `git log --oneline` for the branch reads as a coherent story: each commit
  is one describable change, in the imperative, matching the project's
  existing message convention.
- No commit mixes an unrelated change with the one it claims to make.
- A shared branch was never force-pushed without explicit agreement from
  whoever else has it checked out.
- Before a risky multi-step operation, a checkpoint (a tag, a branch, a
  noted commit hash) exists to return to.
