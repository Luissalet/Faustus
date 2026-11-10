---
name: fresh-context-review
description: Draft with the session that holds all the context (the elephant) and review with fresh workers that have never seen the conversation (the goldfish), so unstated assumptions, missing definitions and holes the long chat has made invisible show up before the document is approved. Faustus runs each goldfish as a read-only delegate_agents worker with no shared context. Use when a spec, design, plan, README, proposal, report or long answer was written after a long conversation and must stand on its own, when the user asks for a review "with fresh eyes", "sin contexto" or "como si no supieras nada", or before handing a document to someone who was not in the chat.
version: 1.0.0
category: planning
tags: [review, fresh-context, goldfish, elephant, context-bias, design-review, spec, delegate-agents, revision, ojos-frescos, pez, elefante]
status: published
source: imported
---

## When to Use

After a long session the drafting agent knows things the page does not say: decisions taken twenty turns ago, names only used in chat, constraints the user mentioned once. Rereading in the same session cannot see those gaps, because the context fills them in. A reviewer that starts empty reads only what is written and trips exactly where a future reader will. Use it for specs and design documents, implementation plans, READMEs and handoff notes, client proposals, research reports, and any long answer the user will forward. Not for a two-line reply or a change already covered by tests.

Adapted from google/elephant-goldfish-model (Apache-2.0, pinned bdcfe8cdaee9), its review and review-loop skills, rewritten for Faustus's own worker tools. See THIRD_PARTY_NOTICES.md.

## Procedure

1. **Elephant: write the document to a file.** Put the draft on disk (workspace file or a Cerebro note) so a reviewer can open it; a draft that only exists in chat cannot be reviewed without passing the chat along. List the source material the document depends on (code paths, data files, the spec it implements, URLs saved as local files): this is the goldfish's reading list.
2. **Pick 2–4 angles, one per goldfish.** Typical ones: *comprehension* (can a newcomer follow it; undefined terms, missing steps), *design critic* (wrong or unjustified decisions, alternatives not considered, failure cases), *codebase fit* (does it match the code that exists: names, paths, routes, data shapes), *implementation readiness* (could someone build or run it from this alone; order, acceptance criteria, verification), *zoom out* (does it solve the stated problem at all). Use more angles only for a big document.
3. **Launch the goldfish with `delegate_agents`.** One task per angle, each with `read_only: true`, a short `name` ("pez: comprensión"), `max_findings` (4–6) and, for a wide review, `tier: "fast"` or a local model to keep it cheap. **Do not pass `context`** and do not paste the conversation, your intentions or your own opinion of the draft into the instruction: anything you add there is the elephant's memory leaking into the goldfish. The instruction contains only: the angle and what to look for, the path of the document, the reading list, and the output format — each finding with severity (blocker, major, minor), location (section or line), what is wrong and why it matters to a reader with nothing else.
4. **Never reuse a goldfish.** A second round is new workers, not a resumed one: a resumed worker carries its earlier transcript and is no longer fresh. Alternatives with the same property: `swarm_map` in `llm` mode over the angles (one bare model call each), or `fanout_run` to get the same review from several models.
5. **Elephant: triage every finding.** With the full context, sort each one: *real gap* (fix the document), *already decided* (the decision exists but is not written: write it down — this is the most common and most valuable kind), *wrong* (the reviewer misread; say why in one line), *out of scope*. Do not dismiss a finding just because you know the answer: if the document does not say it, the reader will not know it either.
6. **Ask the user only about trade-offs.** Findings that need a decision the user owns (scope, cost, priority) go to the user as a short list; everything else the elephant fixes directly.
7. **Repeat until clean.** Apply the fixes to the file, then run a new round of fresh workers on the changed sections. Stop when a round returns no blocker or major finding, or after three rounds with a note of what stays open.
8. **Report** the rounds run, the findings per severity, what was fixed, what was rejected and why, and what the user must decide.

## Pitfalls

- Briefing the reviewer "so it understands": the briefing is exactly the bias the method removes. Point at files; never summarise them.
- Reviewing in the same session and calling it a fresh review. If no separate worker ran, say that the review was done with full context.
- Shared `context` in `delegate_agents` reaches every worker: leave it empty for this skill.
- Giving the goldfish the whole repository: the reading list is what the document cites, plus what a reader would naturally open.
- Treating the goldfish as authoritative. It can be wrong; the elephant decides, with reasons.
- Accepting a finding by rewording instead of adding the missing fact; the next goldfish will trip on the same spot.
- One giant reviewer with every angle produces shallow findings; split angles across workers.

## Verification

- Each review round shows separate worker runs (child chat ids in the `delegate_agents` report) started without `context`.
- Every finding has a recorded outcome: fixed, written down, rejected with a reason, or sent to the user.
- The last round has no blocker or major finding, or the open ones are listed for the user.
- The final document states the decisions that the first round found missing.
