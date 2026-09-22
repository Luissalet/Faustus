---
name: error-handling
description: Typed, specific error handling that fails loudly where it happens and gives the caller (human or code) enough to act on. Use when writing code that can fail — I/O, network calls, parsing untrusted input, or anything that calls another service.
version: 1.0.0
category: engineering
tags: [error-handling, reliability, exceptions]
status: published
source: imported
---

## When to Use

Any code path that can fail for a reason outside your control: a network
call, a file read, parsing input you did not generate yourself, a database
query, a call into another service. Write the error handling alongside the
happy path, not as an afterthought once the feature "works".

## Procedure

1. **Use specific exception/error types, not a generic catch-all.** A
   `ValidationError` and a `NotFoundError` need different handling upstream;
   collapsing them into one generic exception forces every caller to
   re-parse a message string to tell them apart.
2. **Catch only what you can handle at that level.** If a function can add
   useful context (which record, which input) to a failure but can't
   actually recover from it, wrap and re-raise with that context rather
   than swallowing it into a log line and returning a default value.
3. **Never let a bare `except:`/empty `catch` pass silently.** At minimum,
   log with enough detail to diagnose (what operation, what input, what the
   underlying error said) before deciding whether to re-raise, return a
   typed failure, or degrade gracefully — and that decision should be
   visible in the code, not accidental.
4. **Prefer a typed result over throwing across a boundary that expects to
   handle failure as data** (a queue consumer, a batch job that must
   continue past one bad row) — return an explicit success/failure value
   the caller is forced to check, rather than an exception it might not
   expect.
5. **Wrap errors with context as they cross a layer boundary** (from a
   database driver's error, to a domain error, to an HTTP response) so each
   layer adds what it uniquely knows, without leaking internals (a raw SQL
   error, a stack trace, an internal file path) to the outermost caller.
6. **Retry only what is actually transient** (a timeout, a 503, a connection
   reset) and only with backoff and a cap — retrying a validation failure or
   a 4xx just delays the same certain failure.
7. **Write user-facing error messages that say what to do next**, not the
   internal exception text. "That file is too large (max 10 MB)" beats
   "ValueError at line 214".

## Pitfalls

- Catching `Exception` (or the language's broadest error type) "to be
  safe" — it also catches the bugs you actually wanted to see crash loudly
  during development.
- Returning `None`/`null`/a default value on failure instead of raising or
  returning an explicit typed failure — the caller cannot tell "empty
  result" apart from "the call failed" and will eventually treat one as
  the other.
- Retrying without a cap or backoff, turning a transient blip into a
  thundering-herd retry storm against a service that is already struggling.
- Logging the same error at every layer it passes through, producing five
  log lines for one failure with no layer adding new information.
- Exposing internal error text (stack traces, driver messages, file paths)
  directly to an external caller — both a security leak and a UX failure.

## Verification

- Every call that can fail has an explicit decision about what happens on
  failure — visible in the code, not left to whatever the language does by
  default.
- No bare/generic except-and-ignore exists in the diff.
- A user-facing error message was tested by reading it as if you were the
  user, not the author: does it say what to do next?
- A retry loop, if present, has both a cap and backoff, and only wraps
  operations that are actually transient.
