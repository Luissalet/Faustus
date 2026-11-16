---
name: session-retro
description: Read a finished coding session and propose changes to the agent's environment rather than to the code - navigation pointers, automated checks, review rules, slimmer steering files, cheaper tool use, missing information - ordered by severity. Use when asked for a retrospective on a session, when the same mistake or slow search keeps recurring, or when the steering files have grown large.
version: 1.0.0
category: planning
tags: [retrospective, retro, agent-environment, steering, checks, third-party]
status: published
source: imported
---

## When to Use

After a session that was slow, wrong or repetitive, or on request ("do a retro of this session"). The subject is
the environment the agent worked in - the files it reads, the checks that run, the tools it calls - and what to
change so the next run goes better. Not a review of the code the session produced (see `fresh-context-review`)
and not for turning one repeated correction into a rule (see `steer-once`). Adapted from
https://github.com/mattpocock/skills (MIT, © 2026 Matt Pocock).

## Procedure

1. **Read the primary sources.** The session the person names, otherwise the current one: the messages, the tool
   calls and their results, the commits it made. Search saved conversations for it if it is another session. Work
   from what happened, not from what you remember of it.
2. **Keep the two stages apart.** Implementation carries the most context pressure: exploring, writing, debugging.
   Review receives a diff and needs no exploration, so it is the stage that should impose standards. Put a rule
   where the lighter-loaded stage can apply it.
3. **Look for candidates in these categories, each with its trigger:**
   - *Navigation.* How long did it take to find the right files? Are there hidden dependencies between files? A
     one-line pointer from a file the agent always reads to the one it kept missing is the usual fix. Trigger: a
     long search for one fact.
   - *Automated checks.* Which mistakes could a linter, type check, test or file-layout check have caught? Read the
     repository's own check command and its CI workflow first: a check that exists but is unwired or silently
     broken is the finding, not a new one. A repository with no guardrail at all is itself a finding. Trigger: a
     mistake a machine could catch.
   - *Standards.* Was a rule missed in review, or is one unclear or obsolete? Classify the violation first. A
     mechanical one (a banned call, an import shape, a file location) gets a deterministic check, built in the
     repository's own linter, hook or CI. Keep the written standards for judgement calls no check could replace
     ("matches the surrounding style"). Trigger: the reviewer let a mistake through.
   - *Steering files.* What in the always-loaded instruction files could be a check, a standard or a pointer
     instead? Trigger: a large file.
   - *Tool economy.* Which calls were expensive and could be cheaper (a command with huge output, a tool that
     returns whole files)? Trigger: one costly call.
   - *No-ops.* Which instructions did not change the agent's behaviour at all? Cut them. Trigger: bulky steering.
   - *Information access.* What did the agent need and not have: dev-server logs, read access to a service, the
     output of a failed job? Trigger: a crucial fact it could not see.
4. **Quote the evidence for each candidate**: the message or call where the friction showed, and the cost in
   rounds or time. A candidate with no evidence from this session is a guess; drop it or label it.
5. **Rank by severity and present.** For each: the category, the evidence, the change in one sentence, where it
   goes (which file, which check, which tool) and the effort. Order by how much the next run saves. Keep it to
   what matters; a long list of nits hides the three that count.
6. **Offer to make the change, one at a time.** Pointers and checks are small enough to do now. Writing for agents
   follows the same rules as any instruction: short, specific, with the reason, and placed where it will be read.
   Use `manage_instincts` or a project rule for habits, a skill for a procedure, and a check for anything
   mechanical.

## Pitfalls

- Reviewing the code instead of the environment.
- Turning every annoyance into a new written rule when a deterministic check would be cheaper and never forgotten.
- Growing the always-loaded steering files; a retro should usually shrink them.
- Candidates without evidence, or evidence from memory instead of the session record.
- Putting standards on the implementation stage, which is already overloaded, instead of review.
- Proposing a check that already exists but is unwired; wire it instead of writing a second.

## Verification

- Every candidate quotes where in the session it showed and what it cost.
- The repository's existing checks and CI were read before any check was proposed.
- The list is ordered by expected saving, with the category and the target file named.
- The steering files are no longer after the changes than before, unless something essential was missing.
