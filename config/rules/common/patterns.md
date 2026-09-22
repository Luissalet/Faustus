---
id: common/patterns
title: General design patterns
applies_to: []
priority: 14
summary: Typed errors, explicit dependencies, and small interfaces over generic ones.
---

- Use specific, typed error/exception types; never a single catch-all that
  forces callers to string-match a message to tell failures apart.
- Wrap an error with context as it crosses a layer boundary rather than
  letting a low-level detail (a driver error, a raw status code) leak to
  the outermost caller unexplained.
- Prefer composition and explicit dependencies (passed in, not reached for
  globally) over deep inheritance or hidden singleton state — it's what
  makes a unit testable in isolation.
- Design the smallest interface/contract that does the job at a call site,
  rather than depending on a large one and using a fraction of it.
- Retry only what is genuinely transient (timeout, 503, connection reset),
  and always with a cap and backoff.
- Return an explicit typed failure (not `None`/`null`) when the caller must
  be forced to check for one — a swallowed failure that looks like an empty
  success is worse than a crash.
