---
name: rust-patterns
description: Ownership-first design, `Result`/`?` over `unwrap()`, enums for exhaustive state modelling, and small trait boundaries. Use when writing or reviewing Rust code.
version: 1.0.0
category: engineering
tags: [rust, patterns, error-handling]
status: published
source: imported
---

## When to Use

Writing or reviewing Rust code, layered on top of `coding-standards`.

## Procedure

1. **Design around ownership from the start, not as an afterthought.**
   Decide what owns a value versus what borrows it before writing the
   struct; retrofitting ownership onto code fighting the borrow checker is
   far more painful than getting the shape right up front. Use `Cow` when a
   function sometimes needs to own its input and sometimes can just borrow
   it.
2. **Use `Result` and `?` for anything fallible; never `unwrap()`/`expect()`
   in production code paths.** Reserve `unwrap()` for cases genuinely
   proven impossible to fail (a `const` regex compiled once, a slice index
   already bounds-checked) and say why in a comment when you do.
3. **Use `thiserror` for library-facing error types** (structured, matchable
   variants a caller can handle) **and `anyhow`/an application error type
   at the top level** (a boundary that just needs to report and exit/log,
   not be matched on further).
4. **Prefer `Option` combinators (`map`, `and_then`, `unwrap_or_else`) over
   nested `match`** for simple transformations — readable as a pipeline,
   and it's harder to forget a case.
5. **Model states as enums, not booleans or string tags.** A `Connection`
   with a `bool connected` and a separate `Option<Error>` can represent
   nonsensical combinations; an enum `Connected | Connecting | Failed(Err)`
   cannot.
6. **Match exhaustively on business-logic enums — no catch-all `_` arm** for
   anything where a new variant should force every call site to decide what
   it means, rather than silently falling into a default that might be
   wrong for the new case.
7. **Accept generics, return concrete types** at a function boundary (same
   principle as Go's interfaces): a generic parameter for what the caller
   can supply, a concrete return type for what the caller gets back. Reach
   for a trait object (`Box<dyn Trait>`) only when you genuinely need
   runtime polymorphism over heterogeneous types.
8. **Reach for `Arc<Mutex<T>>` deliberately, not by default**, for state
   shared across threads — and keep the locked section as small as
   possible; a lock held across an `.await` point or a long computation is
   a contention bug waiting to be found under load.

## Pitfalls

- `unwrap()`/`expect()` left in a path that can actually fail at runtime
  (parsing external input, a network response) — it turns a recoverable
  error into a process crash.
- A `match` with a catch-all `_` arm on a business-logic enum, silently
  swallowing the case where a new variant is added later and nobody
  updated this match.
- Cloning to dodge a borrow-checker error instead of restructuring
  ownership — it compiles, but it's usually a sign the ownership design
  needs a second look, not a permanent fix.
- Holding a `Mutex` lock across an `.await`, which can deadlock or badly
  serialize an otherwise-async system.
- A trait so large that implementing it (for a real type or a test double)
  requires stubbing methods the actual caller never uses.

## Verification

- No `unwrap()`/`expect()` exists on a path that handles external or
  fallible input, without an explicit comment proving it's actually
  infallible there.
- `cargo clippy` passes on the changed files with no new suppressions
  added to silence it.
- Every business-logic `match` on an enum is exhaustive without a catch-all
  arm, or the catch-all is deliberate and justified in a comment.
- No lock is held across an `.await` point in the diff.
