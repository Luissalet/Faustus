---
id: typescript/testing
title: TypeScript testing
applies_to: [TypeScript]
priority: 36
summary: Test through the public surface, type the test data, avoid snapshot-only assertions.
---

- Test through the module's exported surface, not by reaching into private
  implementation details.
- Give test fixtures a real type rather than an untyped literal object, so
  a shape change surfaces as a type error in the tests too.
- Prefer explicit assertions over a bare snapshot test for anything whose
  correctness matters — a snapshot only proves "it didn't change", not
  "it's right".
- Mock at the actual I/O boundary (fetch, a client SDK); don't mock an
  internal pure function you could just call directly.
- Run the type checker (`tsc --noEmit`) as part of verifying a change, not
  only the test runner — a test can pass with a type error elsewhere.
