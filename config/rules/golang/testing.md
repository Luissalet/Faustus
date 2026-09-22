---
id: golang/testing
title: Go testing
applies_to: [Go]
priority: 36
summary: Table-driven tests, t.Parallel where safe, testify only if already used.
---

- Prefer table-driven tests for multiple input/output cases over a
  separate function per case.
- Mark independent tests `t.Parallel()` when they don't share mutable
  state, to keep the suite fast.
- Use `t.Cleanup` for teardown instead of a manual deferred call scattered
  through the test body.
- Only reach for an assertion library if the project already uses one;
  don't introduce a new one for a single test file.
- Run `go test ./...` (or the narrower package path during the loop)
  and read actual failures, not just the pass/fail summary line.
