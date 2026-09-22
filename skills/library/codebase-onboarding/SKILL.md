---
name: codebase-onboarding
description: Build a working mental map of an unfamiliar codebase in one focused pass — stack, architecture, conventions, entry points — before making changes in it. Use when you are working in a repository for the first time, or returning to one you haven't touched in a long time.
version: 1.0.0
category: research
tags: [onboarding, architecture, exploration]
status: published
source: imported
---

## When to Use

The first real task in a repository you have not worked in before, or a
repository that has changed enough since you last saw it that your old mental
model is unreliable. Not needed for a one-line fix in a file you were just
pointed at directly.

## Procedure

1. **Reconnaissance.** Read the manifest files first: `package.json`,
   `pyproject.toml`/`requirements.txt`, `go.mod`, `Cargo.toml`, `Dockerfile`,
   `docker-compose.yml`. They name the language, the framework, and the
   dependencies faster than reading source. Then list the top-level
   directories and read any existing `AGENTS.md`/`README.md` — someone may
   have already written down what you're about to derive.
2. **Map the architecture.** Find the entry point (`main.py`, `app.py`, an
   `index.ts`, a `cmd/` folder) and trace one request or one command from
   entry to where it touches storage. Identify the layering: routes/
   controllers, business logic, data access — and whether the project
   actually keeps them separate or mixes them.
3. **Detect conventions**, not just describe the stack: naming style, how
   errors are handled and surfaced, how tests are organised and named, what
   the commit history's recent messages look like (a quick `git log
   --oneline -20` says a lot about house style), and what the project
   explicitly avoids (check for a lint config's disabled rules, or a
   CONTRIBUTING file).
4. **Find where things live** for the categories you'll likely touch:
   config, database models/migrations, the test suite's structure, and any
   generated or vendored code that should never be hand-edited.
5. **Write down what you found** — even a short scratch note of stack,
   entry points, layering, and "don't touch" list pays for itself the
   moment the task branches into a second file you didn't expect to need.
   If the repository has no `AGENTS.md`/`CLAUDE.md` yet, consider proposing
   one from what you found (many projects have a helper for exactly this;
   check before writing a longhand version from scratch).

## Pitfalls

- Reading the entire repository file by file instead of following the
  entry-point trace — breadth without a thread to follow produces a lot of
  detail and very little understanding of how the pieces fit.
- Assuming a framework's usual conventions apply without checking — a
  project that uses a framework unconventionally (e.g. routes defined by
  decorator scanning rather than a router file) will mislead anyone who
  assumes the textbook layout.
- Skipping the test suite when mapping the architecture — tests are often
  the most accurate, most current description of intended behaviour,
  more so than any comment.
- Treating a large legacy area as representative of the whole codebase's
  current conventions — check the git history's recency; a five-year-old
  module often uses patterns the rest of the codebase has since abandoned.

## Verification

- You can name, from memory and without re-reading, the entry point,
  the main layers, and the test runner command.
- You can point to a specific example in the codebase for each convention
  you plan to follow (not "React projects usually…" but "this file does
  it this way").
- Before your first edit, you checked whether the file you're about to
  change is generated, vendored, or otherwise off-limits.
