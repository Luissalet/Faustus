---
id: rust/security
title: Rust security
applies_to: [Rust]
priority: 30
summary: Justify every `unsafe`, parameterised queries, no unwrap on external input.
---

- Every `unsafe` block needs a comment stating the invariant that makes it
  safe here — an `unsafe` block with no justification is a review blocker.
- Use a query builder's/driver's own parameter binding for SQL; never
  format user input directly into a query string.
- Never `unwrap()`/`expect()` on a value derived from external input
  (parsed request, file content, network response) — handle the `Err`/
  `None` case explicitly.
- Validate input size/bounds before an allocation driven by an external
  value, to avoid an attacker-controlled allocation size.
