---
id: kotlin/testing
title: Kotlin testing
applies_to: [Kotlin]
priority: 36
summary: JUnit5/Kotest per behaviour, runTest for coroutines, fakes over heavy mocking frameworks.
---

- Use the coroutine test utilities (`runTest`) for suspend-function tests
  rather than blocking with `runBlocking` and a real delay.
- Prefer a hand-written fake implementing an interface over a heavy mocking
  framework when the fake is simple — it's usually easier to read and
  maintain.
- One behaviour per test function, named for the expectation.
- Test a sealed class's handling exhaustively — one test per branch that
  matters, not just the common case.
