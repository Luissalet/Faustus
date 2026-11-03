---
name: game-design-document
description: Write a game design document (GDD) for one game system section by section with the designer — overview, player fantasy, core rules, states, interactions, formulas, edge cases, dependencies, tuning knobs, feel, UI, acceptance criteria — checking every section against existing design docs before writing it. Use when designing a game mechanic or system (combat, movement, flight, economy, progression, AI, camera, UI) for a Unity or other game project, or when turning a game idea into a design doc.
version: 1.0.0
category: planning
tags: [gamedev, unity, game-design, gdd, design-document, mechanics, systems-design, documento-de-diseno, videojuego]
status: published
source: imported
---

## When to Use

A game system needs to be designed or written down before it is built: a movement or flight model, combat and damage, enemies and AI, an economy or loot table, progression, camera and controls, a HUD. Also when a designer's notes need to become a structured document others can build from. Balance review of an existing design is `game-balance-check`; turning a finished design into sprint work is `game-sprint-stories`.

Adapted from Donchitos' game studio skill pack (MIT, pinned 05ad7c8a5b8a, design-system skill); multi-agent routing and config hooks replaced with Faustus tools. See THIRD_PARTY_NOTICES.md.

## Procedure

1. Gather context first: `glob` and `grep` for existing design docs (`design/`, `docs/`, `GDD*`), the game concept or pillars, and docs of systems this one depends on. In a Unity project read the relevant scripts and ScriptableObjects (`unity` tool for the project hierarchy) so the design matches what exists. Read long docs by heading, not whole.
2. Confirm the system's scope with the designer in one `ask_user` question: name, layer (foundation, core, feature, presentation), priority (prototype, vertical slice, full vision), key dependencies.
3. Create the skeleton file (e.g. `design/gdd/<system>.md`) with a status header (status, last updated, pillar it serves) and every section as `[To be designed]`: Summary, Overview, Player Fantasy, Detailed Design (Core Rules; States and Transitions; Interactions with Other Systems), Formulas, Edge Cases, Dependencies, Tuning Knobs, Visual/Audio Requirements, Game Feel, UI Requirements, Cross-References, Acceptance Criteria, Open Questions. Skip Formulas only if the system has no numeric rule.
4. Work one section at a time: question → two or three options with trade-offs → designer's decision → draft → approval → write that section to the file immediately. Never generate the whole document at once and present it as finished.
5. Formulas: name every variable, give units and ranges, show a worked example with real numbers, and list the tuning knob that controls each constant.
6. Edge cases: what happens at zero, at maximum, on simultaneous events, on interruption, on save/load, on network or frame hitches.
7. Cross-check each section against approved docs; a conflict is flagged to the designer with both quotes, never silently resolved.
8. Acceptance criteria are testable statements (a Unity EditMode/PlayMode test or a playtest observation can prove each one).
9. After the last section, list open questions and file follow-up work with `board_create` (one item per open question or prototype needed).
10. If the session stops midway, resume by reading the file: sections with real content are done, `[To be designed]` ones remain.

## Pitfalls

- Writing sections without the designer's decision; inventing pillars or a concept that is not in the project.
- Formulas without units, ranges or a worked example, which nobody can tune.
- Copying numbers from another game as if they were targets.
- Contradicting an approved doc silently.
- Designing beyond the agreed priority (full-vision features in a prototype doc).
- Describing feel with adjectives only; feel needs measurable targets (response time, acceleration curves, camera lag in seconds).

## Verification

- Every scaffolded section has content or an explicit "not applicable because …".
- Every formula has variables, units, ranges and a worked example that computes correctly.
- Each acceptance criterion maps to a concrete test or playtest step.
- Dependencies named in the doc exist as docs or are filed as open questions.
- `grep` the doc for `[To be designed]` returns nothing when it is declared complete.
