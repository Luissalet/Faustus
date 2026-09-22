---
id: swift/patterns
title: Swift patterns
applies_to: [Swift]
priority: 34
summary: Protocol-oriented design, enums with associated values for state, weak references to break retain cycles.
---

- Model a multi-case state as an `enum` with associated values, not a
  struct with several optional fields that can represent an impossible
  combination.
- Define behaviour as a protocol and depend on it, rather than a concrete
  class, wherever a test needs to substitute a fake.
- Use `weak`/`unowned` deliberately on a closure or delegate reference that
  would otherwise create a retain cycle; don't reach for it everywhere out
  of habit.
- Prefer `Result<Success, Failure>` or `async throws` for an operation that
  can fail, over a completion handler with an optional error parameter
  nobody's forced to check.
