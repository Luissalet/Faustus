---
id: java/coding-style
title: Java coding style
applies_to: [Java]
priority: 32
summary: Favour immutability, final fields, records for plain data carriers.
---

- Prefer `final` fields and immutable objects by default; mutate only where
  there's a real reason to.
- Use a `record` for a plain data carrier instead of a hand-written class
  with a constructor, getters, `equals`/`hashCode`/`toString`.
- Prefer constructor injection over field injection for dependencies — it
  makes required collaborators visible and testable without a framework
  running.
- Use `Optional` for a value that may genuinely be absent, at API
  boundaries; avoid overusing it for internal fields.
- Follow the project's existing package-by-feature or package-by-layer
  convention rather than introducing a third pattern.
