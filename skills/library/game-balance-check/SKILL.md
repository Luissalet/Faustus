---
name: game-balance-check
description: Check game balance from data and design targets — combat DPS and time-to-kill per tier, dominant options, economy faucets and sinks, progression dead zones and spikes, loot timing and pity math — and report NOT ASSESSED when the data or targets are missing instead of a false pass. Use when tuning numbers, after playtest feedback about difficulty, or when asked to check balance, difficulty curves or an in-game economy.
version: 1.0.0
category: testing
tags: [gamedev, unity, balance, tuning, economy, progression, difficulty, combat, game-design, equilibrio, dificultad]
status: published
source: imported
---

## When to Use

Numbers in a game need checking: weapon or ability values, enemy health and damage, speed and handling values of a vehicle or flight model, XP and power curves, prices and rewards, drop rates. Usually after `game-design-document` defined targets or after `game-playtest-report` flagged difficulty or pacing.

Adapted from Donchitos' game studio skill pack (MIT, pinned 05ad7c8a5b8a, balance-check skill). See THIRD_PARTY_NOTICES.md.

## Procedure

1. List the inputs this check needs and mark each FOUND or ABSENT: data files (ScriptableObjects, JSON/CSV tables, serialized prefab values — read them with `read_file` or the `unity` tool's serialized properties), the design doc's targets and formulas, playtest numbers if any.
2. If every required input is ABSENT, stop and report `NOT ASSESSED — NO DATA`, naming what was missing and which step produces it. If data exists but no targets, the sections that judge against targets are `NOT ASSESSED`. This is a correct result, not a failure.
3. Identify the domain: combat, economy, progression, loot, or movement/handling.
4. Compare every named value with its target range. Outside the range is an outlier row; a value with no stated range is listed as "no stated range — not judged".
5. Combat: compute DPS and time-to-kill per tier, find strictly dominant options, check defensive stacks for unkillable states and damage-type interactions.
6. Economy: map faucets and sinks with rates, project accumulation over a play session and over ten hours, look for infinite loops, sinks that do not scale with income, items never worth buying.
7. Progression: tabulate the XP and power curves, find dead zones (no meaningful gain for too long) and spikes, check content gates against expected power, and whether grinding or skipping breaks pacing.
8. Loot: expected time to each rarity, pity timer math, items useless at every stage, inventory pressure against acquisition rate.
9. Movement and handling: acceleration, top speed, turn rate and camera response against the feel targets; time to cross a level at expected skill.
10. Do the arithmetic with `run_code` or a small script rather than mentally, and keep the script beside the report so numbers can be recomputed.
11. Report: data sources read; health summary (NOT ASSESSED / HEALTHY / CONCERNS / CRITICAL); outliers table (value, expected, actual, issue); degenerate strategies; progression table; recommendations (priority, issue, suggested change, expected impact); values needing attention with rationale.

## Pitfalls

- A green result from a scan that had nothing to scan; say which happened.
- Judging values against your own taste instead of the design's targets.
- Changing several values at once, which hides which change fixed what.
- Ignoring interactions (two balanced items that combine into a broken one).
- Balancing from averages when variance (crit chance, random drops) drives the experience.

## Verification

- Every number in the report can be recomputed from the saved script and the listed files.
- Every outlier row cites the file and field it came from.
- Each recommendation names the target it moves the value toward and how a playtest or test will confirm it.
- The summary state matches the evidence (no HEALTHY with ABSENT inputs).
