---
id: csharp/patterns
title: C# patterns
applies_to: [C#]
priority: 34
summary: Dependency injection over static access, IDisposable via using, exceptions for exceptional cases only.
---

- Register dependencies through the DI container and inject via
  constructor; avoid static/singleton access to something a test needs to
  substitute.
- Wrap an `IDisposable` in a `using` (or `using` declaration), never a
  manual `Dispose()` call that an exception could skip.
- Reserve exceptions for exceptional, unrecoverable conditions; use a
  result type or a nullable return for an expected "not found" case that
  callers handle routinely.
- Prefer `IEnumerable<T>`/`IReadOnlyList<T>` in a public signature over
  exposing a mutable `List<T>` directly.
- Use `CancellationToken` end-to-end for anything that supports
  cancellation; don't drop it in the middle of a call chain.
