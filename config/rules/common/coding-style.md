---
id: common/coding-style
title: Coding style defaults
applies_to: []
priority: 12
summary: Readable and simple beats clever; match the surrounding file over an abstract ideal.
---

- Optimise for the next reader: no clever one-liner that takes thirty
  seconds to parse when three obvious lines would do.
- Match the project's existing style even where it differs from a personal
  preference — consistency with the surrounding file wins.
- Name things for what they mean (`elapsed_ms`, `is_expired`), not what
  they are (`x`, `flag`).
- Keep a function at one level of abstraction; split it the moment it mixes
  "loop over records" with "format a string" with "open a socket".
- Don't abstract for a use case that doesn't exist yet in this codebase
  (YAGNI). Extract a shared helper after the third real duplicate, not the
  second guessed one.
- Prefer early returns/guard clauses over nesting four `if`s deep.
- A comment explains a non-obvious "why"; it never restates what the code
  already says.
- Never leave a silent `except: pass` / empty `catch` — log or re-raise
  with context.
