---
id: common/karpathy-code-positive
title: Code change discipline (Karpathy, positive short)
applies_to: []
priority: 5
summary: Positive-only short variant of the four code rules, tuned for small models.
---

- State your assumption in one line, then follow the simplest
  interpretation; ask one concrete question only when blocked.
- Write the smallest code that solves the stated problem; reuse what the
  codebase already has.
- Change only what the request needs; match the file's existing style; every
  changed line traces to the request.
- Name the success criterion first (a test, a command), then work until you
  have verified it and report the real output.

Adapted from andrej-karpathy-skills (MIT); positive phrasing for small
models.
