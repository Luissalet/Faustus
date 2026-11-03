---
name: game-sprint-stories
description: Turn a game design or milestone into implementable stories and a sprint plan — one story per testable behaviour traced to the design doc, with acceptance criteria, test evidence, estimate, dependencies and files, then a sprint with a goal, its prerequisites, capacity, must/should/nice tiers, risks and a definition of done, filed on the project board. Use when planning the next sprint or breaking a game feature, epic or design doc into stories for a Unity or other game project.
version: 1.0.0
category: planning
tags: [gamedev, unity, sprint-planning, user-stories, backlog, milestone, agile, estimation, planificacion, sprint, historias]
status: published
source: imported
---

## When to Use

A game design doc, epic or milestone exists and the next step is work items someone can build and verify, or the team needs the next sprint planned from the backlog. Writing the design itself is `game-design-document`.

Adapted from Donchitos' game studio skill pack (MIT, pinned 05ad7c8a5b8a, create-stories and sprint-plan skills); file registries replaced with the Faustus project board. See THIRD_PARTY_NOTICES.md.

## Procedure

1. Read the inputs by section: the design doc (core rules, formulas, acceptance criteria), the current milestone goal, the previous sprint's outcome, open board items (`board_list`), and any risk notes. Read the relevant scripts or scenes in the Unity project so stories name real files.
2. Split into stories: one story per behaviour a player or test can observe, small enough for one to three days. Type each: feature, technical foundation, content, polish, or spike (a timeboxed question with a written answer as its deliverable).
3. Write each story with: title in plain words; the design requirement it implements (doc path and section); acceptance criteria as testable statements; test evidence (EditMode/PlayMode test name, a playtest step, or a screenshot to capture); estimate (S, M, L; split anything larger); dependencies on other stories; files planned to create or change.
4. Review the set once as QA would: every acceptance criterion of the design doc is covered by some story, none is untestable, no story hides two features.
5. Show the stories to the user and ask once for changes (`ask_user`), then file them with `board_create` (type task or feature, one per story) and link dependencies with `board_link`.
6. Plan the sprint: state one sprint goal the player can see or play. List what showing that goal needs (scene, input, art placeholder, build) and check each exists or is a story in this sprint; a missing prerequisite is a story or the goal shrinks.
7. Capacity: available days minus meetings, reviews and a 20% buffer. Fill Must Have (critical path to the goal) first, then Should Have, then Nice to Have, never above capacity.
8. Record carryover from the last sprint with the reason it slipped, risks with an owner and mitigation, external dependencies, and the sprint's definition of done (tests green, build runs, design doc updated, no new critical bugs).
9. During the sprint, report status as done / in progress / not started / blocked with a burndown judgement against days left, and move items on the board (`board_update`).

## Pitfalls

- Stories that restate the design section instead of an observable behaviour.
- Acceptance criteria such as "feels good" without a measurable target or a playtest question.
- A sprint goal that needs an asset, scene or build pipeline nobody planned.
- Filling the sprint to 100% of capacity with no buffer.
- Estimating a story larger than L instead of splitting it.
- Spikes without a timebox or a written outcome.

## Verification

- Every design acceptance criterion maps to at least one story (list the mapping).
- Every story has criteria, evidence, estimate and files; dependencies form no cycle.
- Must Have total fits capacity with the buffer.
- Board items exist for every story and their ids are cited back to the user.
