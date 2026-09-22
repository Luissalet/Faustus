---
name: eval-harness
description: Define capability and regression evals with a concrete grader before building a feature that involves model behaviour, then run them the same way every time so a change's effect is measurable rather than felt. Use when building or changing a prompt, an agent, or any feature whose quality depends on model output rather than deterministic code.
version: 1.0.0
category: testing
tags: [evaluation, agents, benchmarking]
status: published
source: imported
---

## When to Use

Building or tuning a prompt, an agent definition, or a feature whose
correctness depends on model behaviour rather than deterministic logic —
anywhere "it seems to work better now" is the only signal available without
a harness. Also relevant when designing a new agent loop or tool surface:
the action space and the grading contract should exist before the loop is
tuned against vibes.

## How It Works

**Eval types.** A *capability* eval checks whether the new behaviour works
at all on representative cases — the bar to clear before shipping. A
*regression* eval locks in cases that already pass today, so a later change
tuned for a new capability doesn't quietly break an old one.

**Grader types**, cheapest first: a *code-based* grader (does the output
contain X, does the file compile, did the test suite pass) is deterministic
and free — prefer it whenever the success condition can be stated as a
check. A *model-based* grader (a separate call judging the output against a
rubric) is for cases a code check can't capture (tone, whether an answer
actually addresses the question) — keep the rubric narrow and the judge
model different from the one being evaluated where practical. A *human*
grader is the fallback for genuinely subjective quality, used sparingly
because it doesn't scale.

**Metrics.** `pass@k`: does at least one of k attempts succeed — useful when
a human or a retry loop will pick the best of several. `pass^k`: do all k
attempts succeed — the right bar for something that must be reliable every
time, like an automated pipeline step with no human in the loop.

## Procedure

1. **Define the evals before implementing**, not after: name the capability
   cases, the regression cases already known to work, and the metric that
   decides pass/fail. Writing this first prevents quietly redefining
   "success" to match whatever the implementation ends up doing.
2. **Implement** the feature/prompt/agent change.
3. **Run the evals** and read individual failures, not just the aggregate
   pass rate — a 90% pass rate that fails the same case every time is a
   different problem from one that fails a random 10% each run.
4. **Iterate against the harness**, not against a handful of manual spot
   checks — every tuning pass should re-run the same fixed set so improving
   one case is visibly weighed against any regression elsewhere.

## Harness design notes (for building the loop itself)

When the thing under test is an agent loop rather than a single prompt: keep
the action space small and each action's effect observable and bounded
(prefer a handful of well-specified tools over one that does everything);
give every failure a recovery path back into the loop rather than a dead
end; budget context deliberately rather than letting transcript grow
unbounded (see the `context-budget` skill); and benchmark against the same
fixed task set on every change, exactly as above.

## Pitfalls

- Skipping straight to a model-based grader for something a deterministic
  check could answer — slower, costlier, and noisier than it needs to be.
- Judging quality from the aggregate pass rate alone and never reading which
  specific cases fail and why.
- Letting the eval set grow stale — cases that were hard six months ago and
  are now trivially passed no longer tell you anything; retire or replace
  them.
- Using the same model to both produce and grade its own output on a
  subjective rubric, which correlates the judge's blind spots with the
  producer's.

## Verification

- Every capability claim ("this now handles X") has a corresponding eval
  case that actually exercises X, not just a manual demonstration.
- The regression set was run after the change and nothing that passed
  before now fails.
- A model-based grader's rubric is written down, not just implied by
  whatever the grading prompt happens to say this week.
