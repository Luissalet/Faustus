---
id: golang/patterns
title: Go patterns
applies_to: [Go]
priority: 34
summary: Wrap errors with %w, accept interfaces/return structs, bound every goroutine.
---

- Wrap errors with `%w` for context as they propagate; check for a specific
  cause with `errors.Is`/`errors.As`, never by matching the message string.
- Accept the smallest interface a function needs; return a concrete type.
- Give every goroutine a `context.Context` and an actual cancellation path;
  never spawn one with no way to stop it.
- Use a bounded worker pool for fan-out over an unbounded input, not an
  unbounded `go func()` per item.
- `defer` a resource's close immediately after acquiring it, on every
  return path.
- Avoid package-level mutable state; pass dependencies explicitly.
