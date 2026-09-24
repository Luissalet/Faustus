---
name: learn-this-repo
description: Guided, interactive study session through this repository using its own code-graph module map and execution flows -- explains modules in dependency order, asks recall questions, and tracks per-module mastery in a notes file in the workspace. Use when the user says "quiero aprenderme este repo", "explícame este código como si fuera un curso", "enséñame esta base de código paso a paso", "quiz me on this codebase", "teach me this repository", "I want to study this project".
version: 1.0.0
category: research
tags: [learning, code_graph, tutor, onboarding, quiz, mastery]
status: published
source: imported
---

## When to Use

The user wants to build real, checked understanding of a repository over
more than one turn — not a quick architecture summary, but something closer
to a course: explanation, recall questions, and progress that survives
across sessions. Not needed for a single "what does this function do"
question, or a one-pass orientation (see the plain `codebase-onboarding`
skill for that).

## Procedure

1. **Build the study plan from the code graph, not from guessing.** Call
   `code_graph_communities` (level 0) for the module map: each module's
   purpose, key symbols, routes/entry points and coupling. Order modules
   from least-depended-upon toward the hubs everything else calls into.
   Call `code_graph_flows` for the most critical flows and note 2-3 that
   each cross several modules — save these as integration checkpoints
   after the modules they touch have been covered.
2. **Resume or start the notes file.** `read_file` a workspace-root
   `LEARN_REPO_NOTES.md` first — a previous session's mastery table lives
   there. If absent, `write_file` a new one with a per-module table:
   `| Module | Mastery | Last reviewed | Notes |` (mastery ∈ new / seen /
   shaky / solid) plus the planned order, so this session and any future
   one has a single resumption point instead of relying on chat history.
3. **Teach one module at a time, from the real files.** State its purpose
   and how it connects to modules already covered, then `read_file` one or
   two of its actual files for a concrete example — a summary explains the
   map, not the territory, and a paraphrase of `code_graph_communities`'
   own text teaches the summary twice.
4. **Ask 1-3 recall questions before moving on**, answerable from what was
   just explained but not lifted verbatim from it, and wait for the user's
   answer — never reveal it, never ask more than one at a time.
5. **Grade honestly and update the notes file.** Compare the answer against
   what is actually true of the code; update that module's row (tier, date,
   one line on what tripped them up if anything). Only mark a module
   "solid" once it has held across a later reference (a later question or a
   flow-trace), not from a single lucky answer.
6. **Use the saved flows as integration checkpoints** every few modules:
   walk one end to end and ask the user to predict the next hop before
   revealing it — this is what actually tests whether the modules connect
   in their head, not just individually.
7. **Close a session by summarizing the notes file's current state** (solid
   / shaky / not yet covered) and what to cover next time — that file, not
   this conversation, is what a "keep going" or a brand-new session resumes
   from.

## Pitfalls

- Teaching straight from `code_graph_communities`' text without opening a
  real file — a paraphrase of a paraphrase, not a lesson.
- A recall question whose answer was already given in the question.
- Marking a module "solid" from one correct answer instead of requiring it
  to hold up again later.
- Skipping the notes-file read at session start, so the plan restarts from
  module one instead of resuming where the user left off.
- Quizzing on a module not actually explained yet in this session or a
  prior one recorded in the notes file.
- Treating a directory-fallback cluster (small/miscellaneous files grouped
  by folder rather than real coupling — `code_graph_communities` marks
  these) as a real conceptual module; say so instead of teaching it as one.

## Verification

- `LEARN_REPO_NOTES.md` exists in the workspace with one row per module
  taught so far, each with a mastery tier and a date.
- Every module explained this session was followed by at least one recall
  question the user actually answered, not skipped.
- Once three or more modules are covered, at least one cross-module flow
  from `code_graph_flows` was walked as an integration check.
- Asking to resume picks up modules marked new/shaky first, not a restart
  from the beginning of the plan.
