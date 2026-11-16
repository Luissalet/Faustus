---
name: ticket-slicing
description: Break a plan, spec or conversation into tracer-bullet tickets - thin vertical slices through every layer, each declaring the tickets that block it - quiz the person on granularity and blockers, then file them on the project board in dependency order. Use when a plan or spec is ready to be turned into work items, or when a large change has to be split so agents can take pieces in parallel.
version: 1.0.0
category: planning
tags: [planning, tickets, backlog, vertical-slices, tracer-bullet, board, third-party]
status: published
source: imported
---

## When to Use

A settled plan, spec or discussion that must become work items: several people or agents will take pieces, the
change is too big for one context window, or an order of work matters. Not for exploring what to build (use
`pretask-interview` or `spec-driven-development` first) and not for a task small enough to just do. Adapted from
https://github.com/mattpocock/skills (MIT, © 2026 Matt Pocock).

## Procedure

1. **Gather the source.** Work from the conversation, or read the spec or issue the person points at in full,
   comments included.
2. **Look at the code once, if you have not.** Use the project's own domain words in titles and respect recorded
   decisions in the area. Look for prefactoring that would make the real change easy ("make the change easy, then
   make the easy change"); that becomes the first ticket.
3. **Slice vertically.** Each ticket cuts a narrow but complete path through every layer it touches (data, logic,
   interface, tests), not a horizontal slice of one layer. A finished slice can be demonstrated or verified on its
   own, and is sized to fit one fresh context window. Prefactoring comes first.
4. **Give every ticket its blocking edges.** List the tickets that must finish before it can start; a ticket with
   none can start now. Edges are real gates, not a reading order. Parallel slices that touch different areas have
   no edge between them.
5. **Treat a wide refactor as the exception.** One mechanical change whose blast radius fans across the whole
   codebase (rename a column, retype a shared symbol) breaks too many call sites at once to land as a slice.
   Sequence it as expand-contract: first add the new form beside the old; then migrate call sites in batches sized
   by blast radius, each batch its own ticket blocked by the expand; last delete the old form in a ticket blocked
   by every batch. If even batches cannot stay green alone, let them share a branch that all block one final
   integrate-and-verify ticket, and promise green only there.
6. **Quiz the person.** Present a numbered list; for each ticket: a short title, what it is blocked by, and the
   end-to-end behaviour it makes work. Ask whether the granularity is right, whether each edge truly gates, and
   whether any tickets should merge or split. Use `ask_user` when available; iterate until approved.
7. **File them in dependency order, blockers first**, so edges can point at real ids. With the project board:
   `board_create` per ticket (type `task` or `feature`, a short body saying what to build from the user's point
   of view, `acceptance` as one verifiable condition per item), then `board_link` kind `blocked_by` for each edge.
   Cite the ids the board returns; never invent one. Do not close or edit a parent issue.
8. **Say where to start.** Run `board_ready` and name the frontier: the tickets whose blockers are all done. For a
   linear chain that is the first one.
9. **Keep the text durable.** Describe behaviour, not file paths or code, which go stale. The exception is a
   snippet from a prototype that encodes a decision more precisely than prose (a state machine, a schema, a type
   shape): include only its decision-rich part and say it came from a prototype.

## Pitfalls

- Slicing by layer ("the schema ticket", "the UI ticket"): nothing is demonstrable until the last one lands.
- Edges that only reflect reading order, which serialise work that could run in parallel.
- Tickets too large for one context window, or so small each is only a diff.
- Forcing a wide refactor into one slice that cannot be green.
- Filing before the person approved the breakdown, or filing blockers after the tickets that cite them.
- Acceptance criteria that cannot be checked ("works well").
- Writing implementation steps instead of the behaviour the ticket makes work.

## Verification

- Every ticket is a vertical slice that can be verified alone, and has at least one checkable acceptance
  criterion.
- Every edge was confirmed as a real gate; no cycles; `board_ready` lists the tickets expected to start first.
- Prefactoring tickets precede the work they ease, and any wide refactor follows expand, migrate, contract.
- The ids in the reply are the ones the board returned.
