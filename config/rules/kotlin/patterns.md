---
id: kotlin/patterns
title: Kotlin patterns
applies_to: [Kotlin]
priority: 34
summary: Sealed classes for closed state sets, structured concurrency, no GlobalScope launches.
---

- Model a closed set of states/outcomes as a `sealed class`/`sealed
  interface`, and handle it exhaustively with `when` (no `else` that
  silently swallows a new case).
- Launch coroutines in a scope tied to the component's own lifecycle;
  avoid `GlobalScope.launch`, which outlives whatever started it.
- Use `Result<T>` or a sealed outcome type for an operation that can fail
  in an expected way; reserve exceptions for genuinely unexpected failures.
- Prefer immutable `List`/`Map` in a public signature; expose a mutable
  collection only where the caller is meant to mutate it.
