---
id: rust/patterns
title: Rust patterns
applies_to: [Rust]
priority: 34
summary: Model state as enums, Result/? over unwrap, no lock held across await.
---

- Model a value with distinct states as an enum, not a boolean plus an
  `Option` that can represent an impossible combination.
- Use `Result` and `?` for fallible operations; reserve `panic!` for
  genuinely unrecoverable programmer errors.
- Match exhaustively on a business-logic enum; avoid a catch-all `_` arm
  that would silently swallow a new variant added later.
- Never hold a `Mutex`/`RwLock` guard across an `.await` point.
- Accept a generic parameter, return a concrete type, at a function
  boundary; reach for `Box<dyn Trait>` only when you need real runtime
  polymorphism over heterogeneous types.
