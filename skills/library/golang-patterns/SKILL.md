---
name: golang-patterns
description: Idiomatic Go — wrapped errors checked explicitly, small interfaces accepted at the call site, goroutines that always have a cancellation path. Use when writing or reviewing Go code.
version: 1.0.0
category: engineering
tags: [go, golang, patterns, concurrency]
status: published
source: imported
---

## When to Use

Writing or reviewing Go code, layered on top of `coding-standards`.

## Procedure

1. **Never ignore an error.** Every `err` returned gets checked; if there's
   truly nothing meaningful to do, say so explicitly (`_ = f()` with a
   comment why) rather than a silent discard that looks like an oversight.
2. **Wrap errors with context as they propagate**: `fmt.Errorf("loading
   config: %w", err)` so a caller several layers up can still see the root
   cause, and check for a specific error with `errors.Is`/`errors.As`
   instead of string-matching the message.
3. **Accept interfaces, return concrete types.** A function's parameters
   should ask for the smallest interface that does the job (easier to
   satisfy, easier to test with a fake); its return type should be concrete
   (easier for the caller to use directly without an assertion).
4. **Make the zero value useful** where practical — a struct that works
   correctly before any explicit initialization (a `sync.Mutex`, a `nil`
   slice that's safely appendable) reduces constructor boilerplate and
   nil-check bugs.
5. **Give every goroutine a cancellation path.** Pass a `context.Context`
   through anything that does I/O or can run long, and select on
   `ctx.Done()` in any loop that could otherwise run forever. A goroutine
   with no way to be told to stop is a leak waiting to happen.
6. **Use a worker pool with a bounded number of goroutines** for fan-out
   work, not an unbounded `go func()` per item — an unbounded spawn under
   load exhausts memory or downstream connections.
7. **Close what you open, in the right order**, via `defer` immediately
   after acquiring — a file, a lock, a response body. `defer resp.Body.
   Close()` right after the request succeeds, not at the end of a long
   function where an early return would skip it.
8. **Keep interfaces small and defined where they're consumed**, not next
   to the type that implements them — a one-method interface at the call
   site is easier to satisfy with a test double than importing a large
   interface from the producing package.

## Pitfalls

- Ignoring an error return value, especially from something that "usually"
  succeeds (a `Close()`, a `Write()`) — the one time it fails silently is
  the one time data was actually lost.
- String-matching an error message to detect a specific failure instead of
  using `errors.Is`/`errors.As` against a sentinel or typed error — it
  breaks the moment the message wording changes.
- A goroutine started with no context and no way to signal it to stop,
  leaking for the lifetime of the process once its caller has moved on.
- An unbounded `go func()` in a loop over an unbounded input, instead of a
  worker pool with a fixed concurrency limit.
- A large, package-level interface that forces every implementer (real and
  test double) to satisfy methods the actual caller never uses.

## Verification

- Every returned error is checked or explicitly, visibly discarded with a
  reason.
- Every goroutine that can run for a while accepts a `context.Context` and
  actually respects cancellation on it.
- `go vet` (and `staticcheck` if configured) passes on the changed files.
- A resource acquired in a function is deferred-closed immediately after
  acquisition, on every return path including error returns.
