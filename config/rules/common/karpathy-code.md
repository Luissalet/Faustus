---
id: common/karpathy-code
title: Code change discipline (Karpathy, original)
applies_to: []
priority: 5
summary: Think before coding, smallest change, surgical edits, verify against a stated goal. Original four-rule variant.
---

- Think before you code. State your assumptions in one line and follow the
  simplest interpretation; if something is genuinely ambiguous and blocks
  you, ask one concrete question instead of guessing. Do not hide confusion.
- Simplicity first. Write the minimum code that solves the stated problem:
  no speculative features, no single-use abstractions, no error handling
  for cases that cannot happen here.
- Surgical changes. Touch only what the request needs; match the
  surrounding file's style; mention dead code or comments you notice but
  leave them alone. Every line you change traces back to the request.
- Goal-driven execution. Define the success criterion before you start
  (a test that reproduces the bug, a command that must exit 0) and keep
  working until you have verified it, reporting the real output.

Adapted from andrej-karpathy-skills (MIT); rule 1 reworded for unattended
work: declare assumptions and follow the simplest interpretation, ask only
when blocked.
