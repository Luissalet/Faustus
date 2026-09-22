---
id: rust/testing
title: Rust testing
applies_to: [Rust]
priority: 36
summary: Unit tests in-module, integration tests in tests/, no unwrap in test helpers you rely on.
---

- Keep unit tests in a `#[cfg(test)] mod tests` block next to the code they
  cover; put cross-module integration tests under `tests/`.
- Use `Result`-returning test functions with `?` for setup that can fail,
  rather than `unwrap()`ing every fallible setup step.
- Test error paths explicitly (assert on the error variant), not just the
  success path.
- Run `cargo test` for the narrow crate/module during the loop, the full
  workspace before reporting done.
