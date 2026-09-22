---
id: common/development-workflow
title: Development workflow defaults
applies_to: []
priority: 20
summary: Scope explicitly, plan before large changes, verify before reporting done.
---

- State the goal and the success condition in one sentence before starting
  anything non-trivial; name what's explicitly out of scope.
- For a large or risky change, write a short plan and get it confirmed
  before implementing — re-planning after a false start costs more than
  planning up front.
- Prefer small, focused edits over rewriting a whole file; keep the
  existing formatting and line endings.
- Split independent pieces of work across files that don't overlap rather
  than one large edit touching everything at once.
- Run the verification loop (build, type-check, lint, test, diff read)
  before reporting a change complete — see the `verification-loop` skill.
- If something goes sideways mid-task, stop and re-plan rather than pushing
  through with the original plan.
