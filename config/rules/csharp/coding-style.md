---
id: csharp/coding-style
title: C# coding style
applies_to: [C#]
priority: 32
summary: Async suffix on Task-returning methods, records for immutable data, nullable reference types on.
---

- Suffix a `Task`-returning method with `Async`, and await it — never
  block on it with `.Result`/`.Wait()`.
- Enable nullable reference types and treat a new nullable warning as a
  real signal, not noise to suppress.
- Use a `record` for an immutable data carrier instead of a mutable POCO
  with public setters.
- Use `var` when the type is obvious from the right-hand side; use the
  explicit type when it isn't.
- Follow the project's existing namespace-per-folder convention.
