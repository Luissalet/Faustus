---
id: typescript/patterns
title: TypeScript patterns
applies_to: [TypeScript]
priority: 34
summary: Discriminated unions for state, Result-style returns for expected failures.
---

- Model a multi-state value (loading/success/error) as a discriminated
  union, not independent booleans that can represent an impossible
  combination.
- Return a typed `Result`/`Either`-style value for an expected, recoverable
  failure; reserve `throw` for genuinely exceptional, unrecoverable cases.
- Prefer small, focused function signatures over an options object with
  many optional fields whose valid combinations aren't obvious from the
  type alone.
- Narrow a union with a type guard function rather than repeated inline
  `typeof`/property checks scattered across call sites.
- Keep async error handling explicit — a rejected promise that's never
  awaited or `.catch()`ed fails silently.
