---
id: csharp/testing
title: C# testing
applies_to: [C#]
priority: 36
summary: xUnit/NUnit with async test methods, mock at the interface boundary.
---

- Write async test methods for async code under test (`async Task`, not a
  sync wrapper that blocks on the result).
- Mock behind an interface at the actual external boundary (a repository,
  an HTTP client); avoid mocking a concrete class with no interface just to
  make it "testable".
- Use `[Theory]`/parameterised tests for multiple input cases instead of
  duplicated test methods.
- Isolate integration tests that hit a real database/service behind their
  own category/trait so the fast unit suite can run independently.
