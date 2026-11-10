---
name: measured-improvement
description: Make something measurably better without the user acting as the stopwatch — build the feedback loop first as a Loop Card (metric, instrument the agent runs itself, noise floor, pre-registered keep bar, guard, stop rule), then test ideas as paired experiment arms against a control that does the same work, keep only what clears the bar outside the noise, record every rejected idea, and let loop_improve propose, isolate, measure and apply changes. Use when asked to speed up, optimise, reduce, tune or "make X better", when the user pastes numbers or screenshots by hand twice, says "sigue igual" twice, or a win is claimed from a single run.
version: 1.0.0
category: engineering
tags: [loop-card, feedback-loop, benchmark, experiment-arms, a/a, noise, keep-bar, ledger, optimisation, performance, loop_improve, bucle de mejora, medir, rendimiento]
status: published
source: imported
---

## When to Use

An agent with no trustworthy signal keeps changing code and asks the human whether it worked; the human becomes the test runner. Before iterating on speed, size, cost, error rate or quality, find the loop. Signs you are late: the user has pasted two measurements, "still broken / still slow" came up twice, you were about to write "try it now", or someone called a single run a win. Not for a one-shot fix that an existing test already proves.

Adapted from github/lever (MIT), skills find-the-loop and experiment-arms, mapped onto Loop Cards, `loop_arms` and `loop_improve`. See THIRD_PARTY_NOTICES.md.

## Procedure

1. **Name the felt outcome** in the user's words ("el arranque tarda", "PR time-to-green"), and a guard that must not get worse (tests passing, output identical, memory).
2. **Find or build the instrument and run it yourself.** Look for an existing benchmark, test or script first. The command must print the number (or use its run time) and exit 0. If you would ask the user to look, build a probe instead.
3. **Create the card** with `loop_card_create`: command, how to read the number, direction, `repeats` (5 for anything timed), folder. It measures the baseline and the noise (variation, threshold). If it warns the metric is noisy, steady it (fixed input, warm-up, more repeats) before going on.
4. **Pre-register the bar** with `loop_card_plan` before looking at any change: `keep_bar_pct` (e.g. 5) or `keep_bar_abs`, `min_pairs` (6; 10+ for a headline number), `guard_command` (the tests), `stop_rule`, `timebox_min`. Changing it later is logged with a reason.
5. **Test known ideas as arms.** Several candidate commands or folders → `loop_arms` measures each against the card's own command in paired, shuffled rounds with an A/A control. Read the verdicts: KEEP (won every pair, reached the bar, interval excludes zero), MINOR, DROP, INCONCLUSIVE (add rounds or drop it). A popular tip gets an arm like any other.
6. **Or let Faustus propose.** `loop_improve` with the card id and the files that may change: it writes N distinct hypotheses (skipping ideas the ledger already rejected), one fresh worker per proposal, isolates each in an alternative beside an untouched control, runs the guard, measures in paired rounds and merges only the best KEEP, then re-measures the real folder. Use `iterations` > 1 to climb further; poll with `action: status`.
7. **Separate proposer and judge.** The instrument decides, not the agent that wrote the change. One idea per arm.
8. **Report** as `metric: before → after (Δ%)` with the number of pairs, the verdict and what was rejected; the card's ledger keeps it all.

## Pitfalls

- Comparing against an old number instead of a control measured in the same rounds.
- A cheap control (cached, skipped work) that makes every arm look slow or fast.
- Calling a difference inside the noise a win; five runs that "improved" with no change at all is how you find n is too small.
- Bundling three ideas in one arm: when it wins nobody knows which one helped.
- Moving the keep bar after seeing results.
- Retrying a recorded negative without a new reason.

## Verification

- The card shows a baseline with its noise and a keep bar set before the first arm.
- Every kept change has a KEEP verdict with at least `min_pairs` pairs, and the real folder re-measured after applying it.
- The ledger lists every attempt, including drops, with numbers.
- The guard command passed on the kept change.
