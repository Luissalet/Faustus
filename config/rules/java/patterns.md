---
id: java/patterns
title: Java patterns
applies_to: [Java]
priority: 34
summary: Checked exceptions for recoverable cases, interfaces at the boundary, no null return.
---

- Return an `Optional` or throw a specific exception instead of returning
  `null` from a method whose caller needs to distinguish absence from
  failure.
- Define an interface at the boundary a component is consumed from,
  sized to what that consumer actually needs.
- Use a checked exception for a condition the caller can meaningfully
  recover from; an unchecked one for a programming error that shouldn't be
  silently caught and continued past.
- Close a resource with try-with-resources, never a manual `close()` call
  that a thrown exception could skip.
- Favor dependency injection over static factories/singletons for anything
  that needs to be swapped in a test.
