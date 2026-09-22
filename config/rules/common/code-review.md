---
id: common/code-review
title: Code review defaults
applies_to: []
priority: 18
summary: Read the whole diff, not just the parts you meant to change, and check the seam between pieces.
---

- Read the full diff end to end before calling anything done, including
  lines you didn't expect to touch — an edit tool can produce a stray
  change the author never intended.
- Look for the defect a diff hides: a caller not updated for a signature
  change, a name that now means two things, an error path that swallows
  what it should report.
- Check the seam between two independently-written pieces (e.g. two
  delegated sub-tasks), not just each piece in isolation — that's where
  integration bugs live.
- A review that can't write should say so plainly rather than sneaking in
  a "quick fix" alongside its findings.
- Give evidence, not a verdict alone: what you checked, how, and what a
  failure would look like if it existed.
- Flag a test that asserts the behaviour it just implemented rather than
  the behaviour that was actually asked for.
