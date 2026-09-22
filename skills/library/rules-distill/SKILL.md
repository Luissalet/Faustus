---
name: rules-distill
description: Read a repository's actual conventions out of its code, tests, and history, and write down only the ones that are real and non-obvious as short rule files under `.faustus/rules/`. Use when a project has accumulated conventions nobody wrote down, or when an existing `.faustus/rules/` set has drifted from what the code actually does.
version: 1.0.0
category: planning
tags: [conventions, documentation, rules]
status: published
source: imported
---

## When to Use

A codebase you (or a teammate) keep re-deriving the same conventions for
every session, or a project whose `AGENTS.md`/`.faustus/rules/` files are
stale relative to what the code now actually does. Worth revisiting
periodically, not just once at project start.

## Procedure

1. **Inventory what already exists.** List any current rule files
   (`.faustus/rules/`, an `AGENTS.md`, a style guide) and any skills already
   installed for this project, so you don't rewrite something already
   captured correctly.
2. **Collect real evidence, not impressions**: recent commit messages
   (`git log --oneline -50`) for naming and scope conventions, the test
   suite's structure for testing conventions, the lint/format config for
   what's actually enforced vs. merely suggested, and a sample of 3-5 recent
   files in the area you're distilling for actual patterns in practice.
3. **Extract candidates**, each stated as a concrete, checkable rule: "new
   endpoints live under `routes/<area>_routes.py` and register via a
   `setup_<x>_routes()` function" is a rule; "write clean code" is not.
4. **Match each candidate against what you already found in step 1.** A
   candidate that duplicates an existing rule file gets merged or dropped;
   one that contradicts an existing rule needs a verdict on which one is
   actually current (check the more recent commits).
5. **Give every surviving candidate a verdict with evidence**: which files
   it was observed in, and how consistently (if 8 of 10 recent files do it
   one way and 2 don't, say so — that's a real convention with known
   exceptions, not a universal law).
6. **Exclude** anything that's a one-off, anything the linter already
   enforces mechanically (no need to restate what a tool already checks),
   and anything so obvious it wastes the injected budget stated in
   `context-budget`.
7. **Write the survivors as short rule files** under `.faustus/rules/`
   (frontmatter + a handful of imperative bullets, same shape as this app's
   own bundled `config/rules/` library) and present the list — with
   evidence — for a quick human confirmation before treating them as
   settled policy, since a wrong "rule" injected into every future turn is
   worse than no rule at all.

## Pitfalls

- Writing a rule from a single observed instance instead of checking it
  holds across several recent files — one file is an example, not a
  pattern.
- Restating what the linter/formatter already enforces automatically,
  wasting the rules budget on something that needs no reminder.
- Writing rules so generic they could apply to any codebase ("write tests",
  "handle errors") instead of this specific project's actual, checkable
  conventions.
- Skipping the "what already exists" inventory and duplicating or
  contradicting a rule file that's already there.
- Treating a stale convention (present in old code, abandoned in anything
  recent) as still current just because it's still technically observable
  somewhere in the tree.

## Verification

- Every distilled rule cites the evidence it was drawn from (files,
  commits) and would let someone spot-check it in under a minute.
- No distilled rule merely restates something a configured linter/formatter
  already enforces.
- The distilled set was checked against any existing `.faustus/rules/`
  content for duplication or contradiction before being written.
