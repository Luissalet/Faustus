---
name: context-budget
description: Track what is actually consuming the context window, cut what is not earning its space, and know when a long session is worth compacting rather than continuing to grow. Use when a session has read many files, run many tool calls, or is starting to feel slow or repetitive.
version: 1.0.0
category: planning
tags: [context, budget, compaction, efficiency]
status: published
source: imported
---

## When to Use

A session that has been running a while: dozens of tool calls, several large
files read in full, or a task that keeps circling back to information it
already gathered. Also worth a deliberate check before starting a large
delegated job, so the plan is built on a clear picture of what is already
known rather than re-discovering it per sub-agent.

## Procedure

1. **Inventory.** Rough-count what is occupying the window: full file reads
   (especially large ones read more than once), tool outputs that were never
   trimmed, and any long transcript from an earlier sub-task that is still
   being carried around verbatim.
2. **Classify.** Split what you are holding into: facts still needed for the
   remaining work, facts that were needed but are now settled (can be
   summarised to one line), and facts that turned out to be irrelevant
   (drop entirely). Most sessions are carrying more of the last two
   categories than they think.
3. **Cut before you add.** Before reading another large file, ask whether a
   targeted search (grep for the symbol, read only the function, not the
   module) would answer the same question for less. Re-reading a whole file
   "to be sure" after you already extracted the one line you needed is the
   single most common waste.
4. **Summarise instead of repeating.** When a long tool output (a test run,
   a build log) has already told you what you needed, keep the one-line
   verdict and let the raw output age out rather than quoting it again in
   full in a later message.
5. **Recognise when to compact.** A session is a good compaction candidate
   when: the task has moved to a genuinely new phase (planning is done,
   implementation is starting), the early exploration is no longer relevant
   to what remains, or the window is visibly full of transcript that is
   history rather than working material. Compacting mid-investigation, before
   you know what actually matters, throws away context you have not yet
   learned you need.
6. **Preserve the load-bearing parts across a compaction**: the actual goal
   in the user's words, decisions already made and why, file paths and
   symbols already located, and anything a fresh read would be expensive to
   re-derive. Everything else — the play-by-play of how you got there — can
   go.

## Pitfalls

- Reading an entire large file when a `grep`/`glob` for the one symbol you
  need would do, "just in case there's something else relevant" — that
  instinct is usually wrong and it is expensive every time it is wrong.
- Carrying a sub-agent's full transcript forward instead of its final
  report — the report is the interface; the transcript is that worker's own
  scratch space.
- Compacting reflexively on a timer rather than at a real phase boundary,
  which risks losing detail you turn out to need one message later.
- Treating "the window still has room" as "no action needed" — a context
  window that is technically not full but 80% history is still degrading
  the model's ability to find the 20% that matters.

## Verification

- After a deliberate cut, the task can still be described accurately from
  what remains — if a fact you needed got dropped, the cut was too
  aggressive.
- A compaction, when done, keeps the literal goal, key decisions, and
  located file paths intact; ask "what was I doing and why" immediately
  after and confirm the answer is still right.
