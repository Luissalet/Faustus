---
name: steer-once
description: Turn a correction the user keeps repeating into a durable fix at the cheapest layer that would have prevented it — an instinct or project rule, a skill rule, a test that fails today, a check, a scheduled task, or a bug report — with the evidence (count, chats, short redacted quotes), then re-measure after a week and keep, climb one layer, or revert. Use when the user says "otra vez", "ya te lo dije", "why did you stop", "didn't I give you a skill for this", or prompt_mirror reports the same correction class in three or more chats.
version: 1.0.0
category: planning
tags: [corrections, rules, instincts, project-rules, prompt_mirror, feedback, recurring, durable-fix, reglas, instintos, correcciones]
status: published
source: imported
---

## When to Use

An apology changes nothing in the next chat; a rule does. When the same correction comes back — stopping early, asking instead of doing, inventing approval gates, claiming something without checking the live source, patching a symptom, shrinking the ask, side quests, "still broken" with nothing measured — find the layer that would have prevented it and put the smallest durable change there. Not for a one-off preference or a single bad turn.

Adapted from github/lever (MIT), skills steer-once and prompt-mirror, mapped onto `prompt_mirror`, instincts and project rules. See THIRD_PARTY_NOTICES.md.

## Procedure

1. **Collect.** Live: the correction the user just repeated. Batch: run `prompt_mirror` (30 days); read its repeated-correction phrases, the classes with three or more chats, and the assistant concessions ("Tienes razón…") that were followed by the same correction again — the strongest sign a rule is missing or not loaded.
2. **Classify** by the expectation behind it (premature stop, over-asking, gate, stale claim, shallow fix, letter of the law, scope creep, still broken, repeat) and merge near-duplicates: one rule per expectation.
3. **Look for an existing rule** (`manage_instincts` list, `.faustus/rules/`, project instructions, the skill involved). A rule that exists while the correction still recurs is weak, misplaced or not loaded: fix its wording or place before adding another.
4. **Pick the cheapest layer that would have prevented these instances:** a personal posture → an instinct; a project default → a project rule; a procedure → the rule inside the relevant skill; something detectable → a test or check that fails today; timing or liveness → a scheduled task or a watchdog; outside the agent's control → a bug report.
5. **Write it positive, concrete, at most three lines, with the why** ("When an item lands, take the next ready one; stop only at the done-when. Why: 'por qué paras' in 4 chats."). No capital-letter walls, never a gate to prevent gates.
6. **Show the evidence before saving** anything shared: class, window, count per 100 typed messages, chats, 3–5 redacted quotes of ≤20 words.
7. **Re-measure after about a week** with `prompt_mirror`: the class rate fell beyond the week-to-week spread → keep; did not fall → climb one layer; a neighbouring class rose → reword or revert, with numbers.

## Pitfalls

- Accepting "You're right" as the fix.
- Prose rules for what a test, a check or a setting should own.
- Over-broad rules that create a new correction class.
- One person's posture written into a shared repository.
- Quoting heated or sensitive messages; use counts.

## Verification

- The rule names the correction class, its count and the date it was set.
- It is stored where the next chat loads it (instinct, rule file or skill) and was read back.
- A re-measure date is recorded, and the next prompt_mirror run reports the class rate against the baseline.
