---
id: common/git-workflow
title: Git workflow defaults
applies_to: []
priority: 22
summary: One logical change per commit, imperative message, never force-push a shared branch.
---

- Write commit messages in the imperative, one logical change per commit;
  follow the project's own message format if it has one.
- Explain *why* in the commit body, not what — the diff already shows what
  changed.
- Never mix an unrelated drive-by fix into a commit about something else.
- Never force-push a branch other people have already pulled without
  explicit agreement.
- Rebase a not-yet-shared feature branch onto the latest base branch to
  keep history linear; merge (don't rebase) once it's shared with others.
- Before a risky multi-step git operation, note a checkpoint (a tag, a
  branch, a commit hash) to return to.
- Check `.gitignore` before committing — never commit generated files,
  build artifacts, or credentials.
