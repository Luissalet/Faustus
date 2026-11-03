---
name: game-playtest-report
description: Prepare a playtest and turn its raw notes into a structured report — session info, test focus, first five minutes, what worked, pain points, confusion, delight, bugs with severity, feature feedback, numbers, overall assessment and top three priorities — then route each finding to design change, balance, bug or polish. Use when planning a playtest, writing up playtest notes or tester feedback, or deciding what to fix after a playtest of a game build.
version: 1.0.0
category: testing
tags: [gamedev, unity, playtest, qa, feedback, user-testing, game-design, prueba-de-juego, testers]
status: published
source: imported
---

## When to Use

Before a playtest (to give the tester session a focus and a blank report), and after it (to turn notes, recordings, chat messages or a form export into a structured report and actions). Balance questions raised by the report go to `game-balance-check`; design changes go back to `game-design-document`.

Adapted from Donchitos' game studio skill pack (MIT, pinned 05ad7c8a5b8a, playtest-report skill); director gates replaced with a single review against the game's pillars. See THIRD_PARTY_NOTICES.md.

## Procedure

1. Ask which mode applies (`ask_user`): a new blank report for an upcoming session, or analysing existing notes (get the path).
2. New report: produce the template with these sections — Session Info (date, build/commit, duration, tester id, platform, input method, first-time/returning/targeted); Test Focus (the hypothesis being tested); First Impressions in the first five minutes (understood the goal? the controls? emotional response); Gameplay Flow (what worked, pain points with severity, confusion points, moments of delight); Bugs Encountered (table: description, severity, reproducible); Feature-Specific Feedback; Quantitative Data (deaths and where, time per area, features found vs missed); Overall Assessment (play again, difficulty, pacing, session length); Top 3 Priorities. Stop there: a blank template is not saved or routed.
3. Analyse mode: read the raw notes (and any video with `inspect_media`, screenshots with `inspect_image`). Read the design doc sections for the features tested so observations are judged against the intended experience.
4. Fill the template only with what the notes support; mark sections with no evidence as "not observed". Quote the tester where possible.
5. Put defects (crash, frame-rate drop, softlock, something that plainly breaks) in the Bugs table, not under pain points; pain points are about how the game feels.
6. Route every finding into one bucket: design change (confusion, broken fun, conflict with the design intent), balance adjustment (numbers feel wrong, difficulty spike or flatline), bug (reproducible defect), polish (friction for later).
7. Review the whole report once against the game's pillars or one-line pitch: does the session support or contradict them? If no pillars exist, say the review is against the test hypothesis only.
8. Save the report beside the project's QA notes (e.g. `qa/playtests/playtest-<date>-<tester>.md`) after confirming the path, and file bugs and design changes with `board_create`, citing the ids.

## Pitfalls

- Filling empty sections with plausible guesses; absent evidence is "not observed".
- Mixing defects with feel feedback, so bugs never reach the bug list.
- Treating one tester's preference as a design verdict; note sample size.
- Leading the tester during the session (explaining controls) and then reporting that the controls were understood.
- Losing the build id, which makes every finding unreproducible.

## Verification

- Every finding cites a line of the notes, a timestamp in a recording or a screenshot.
- Every bug row has severity and reproducibility; every routed item has a board id.
- Top 3 priorities each map to a bucket and an owner.
- The report names the build and the hypothesis it tested.
