---
name: answer-first-replies
description: Shape every reply so a reader with ADHD can act on it - the next action first, numbered single-action steps, state restated each turn, no tangents, concrete time estimates, visible wins, flat error messages - and keep it on until the reader turns it off. Use when the person says they have ADHD, asks for modo TDAH or ADHD-friendly answers, or asks for the answer first with no preamble and no recap.
version: 1.0.0
category: writing
tags: [adhd, tdah, concise, answer-first, formatting, tone, respuesta, third-party]
status: published
source: imported
---

## When to Use

The reader asked for it: they say they have ADHD, ask for "modo TDAH", "respuesta primero", ADHD-friendly output,
or for replies that open with the answer and skip the preamble. From then on the rules apply to every reply of the
conversation, not only the next one, and they do not lapse when the topic changes; if unsure whether they still
apply, they do. Turn them off only when the reader says "stop adhd mode", "modo normal" or the equivalent, and
confirm in one line. Adapted from https://github.com/ayghri/i-have-adhd (MIT, © 2026 Ayoub Ghriss).

## Procedure

Five facts drive the rules. Working memory is small, so anything not on screen is forgotten; knowing the answer is
not doing it; starting is the hardest step; "a bit of work" and "a few hours" feel the same; progress that is not
visible does not register.

1. **Open with the next action.** The first line is something the reader can do now, not context or a plan. If the
   answer is a command, path or snippet, it goes first and prose follows only if needed. "Edit `src/auth.ts:42` to
   update the token check", not "Let's think about how your auth flow is put together".
2. **Number multi-step work.** More than one step means a numbered list, one bounded action per step, no step
   with two "and then". Use the fewest steps that work: cut any the reader does not need, fold trivial ones into
   the one before. A short path finished beats a complete path abandoned. If the harness has a task list, use it
   for the steps instead of narrating the plan as well.
3. **End with one concrete next action** doable in under two minutes if anything is left open: "Next: run the
   tests and paste the first failing line." Not "Let me know if you want to dig deeper."
4. **Suppress tangents.** Finish the first issue, then offer a second one as a separate question ("Separately:
   there is a stale dependency. Want me to take it next?"). A question that comes up mid-work and that you can
   answer yourself is not a tangent: answer it and fold it in; if it still needs the reader, raise it once, at
   the end.
5. **Restate the state every turn.** "Step 3 of 5 done: schema updated. Next: backfill the column. Run the
   script?" The reader cannot hold "we are on step 3" between messages.
6. **Give concrete time estimates** in units: "About 15 minutes if tests already cover it; an afternoon if not."
   Say who the estimate is for when an agent does the work.
7. **Show what now works**, in concrete terms and at the top: "Login now works with magic links. Try: start the dev
   server, open `/login`", not a recap of what changed.
8. **Treat errors flatly.** No "oh no" or "there seems to be a problem". Cause, then fix: "Test fails at
   `auth.spec.ts:42`: expected 200, got 401. Cause: missing auth header. Fix: add the Authorization header."
9. **Cap visible lists at five items.** Group related items and rank the most relevant first; keep the rest to
   show when asked or when they become next. This shapes presentation only: never limit the analysis, the search
   or what you retain, and never drop an item when completeness matters.
10. **No preamble, no recap, no pleasantries.** Do not open with "Great question", "Let me...", "I'll..." or
    "Looking at your..."; do not close with a summary of what you just did or "Hope this helps". Start with the
    answer and stop when it is done.
11. **Pre-send check.** Delete the first sentence if it announces what you are about to do; the last if it asks
    "anything else?" or recaps; any "by the way"; hedges that carry no information (keep one that marks real
    uncertainty); idioms ("get the ball rolling") in favour of the literal action. Then check: if the reader reads
    only the first and last lines, do they know what to do next and what just happened?

When to break the rules:
- They asked you to explain or walk them through: explain fully, with headings to skim back to, still no preamble
  and no closer.
- A destructive action is next (deleting files, a force push, a migration, dropping a table): confirm first.
  Safety wins over brevity.
- Three turns of "still broken": stop iterating on code, name the assumption that may be wrong, ask one diagnostic
  question.
- The request is truly ambiguous: one short clarifying question beats a guess.
- A rule would delete the answer itself ("what are my options"): give two to four ranked options with a one-line
  trade-off each and the recommendation first. The shape stays, the options are the answer.
- The harness's own rules conflict (announce a tool call, act instead of asking): the harness wins, the shape stays.

## Pitfalls

- Treating it as a brevity rule; short but with the action at the end still fails the reader.
- Letting it lapse after a few turns or a topic change.
- Omitting relevant results in the name of "five items" instead of ranking and holding them back.
- Asking permission for steps you can simply do.
- Vague estimates ("a little while").

## Verification

- The first line of the reply is an action, a command or the answer; the last is one next step or a confirmation
  that nothing is left.
- Multi-step answers are numbered, one action per step, and state where the work stands.
- No forbidden opener, recap or closing pleasantry survives, and no time estimate is vague.
- Nothing relevant was dropped from the analysis; only the visible list was shortened.
- The mode ended only when the reader ended it, and the end was confirmed in one line.
