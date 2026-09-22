---
id: golang/security
title: Go security
applies_to: [Go]
priority: 30
summary: Parameterised queries via database/sql, validate before exec.Command, check every error.
---

- Use `database/sql` placeholders for query parameters; never build SQL by
  `fmt.Sprintf`-ing user input into the string.
- Validate/allow-list an argument before passing it to `exec.Command`;
  never build a command string executed through a shell.
- Check every returned `error`; an unchecked `err` on a security-relevant
  call (e.g. a TLS verification step) can hide a real failure.
- Use `crypto/rand`, not `math/rand`, for anything security-sensitive
  (tokens, session IDs).
- Set explicit timeouts on any `http.Client`/`context.Context` used for an
  outbound call — an unbounded call is both a reliability and an
  availability risk.
