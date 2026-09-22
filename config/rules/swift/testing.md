---
id: swift/testing
title: Swift testing
applies_to: [Swift]
priority: 36
summary: XCTest per behaviour, inject dependencies for testability, avoid sleeping in async tests.
---

- Write one behaviour per `XCTest` method with a name that states the
  expectation, not just the method under test.
- Inject dependencies (a protocol, a closure) rather than reaching for a
  singleton, so a test can substitute a fake.
- For async code, use the test framework's async expectations rather than
  a fixed `sleep`/`DispatchQueue.asyncAfter` to wait for a result.
- Reset shared/static state between tests if the project has any, so test
  order doesn't affect the outcome.
