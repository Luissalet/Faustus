---
id: common/security
title: Security defaults
applies_to: []
priority: 10
summary: Never trust input, never build a query/command by concatenation, never leak a secret.
---

- Validate every value that enters from outside the process (request body,
  query param, file upload, webhook, CLI arg from another program) before
  using it — reject unexpected shape, don't silently coerce it.
- Never build a SQL query, a shell command, or an HTML fragment by string
  concatenation with untrusted input. Parameterised queries, argument lists
  (never `shell=True`), autoescaping templates.
- Never put a secret (key, token, password) in source, a commit message, or
  a log line. Use the app's own settings/env mechanism.
- Check authorization per-record, not just per-login: an authenticated user
  reaching another user's record by changing an ID is the most common real
  break.
- Resolve any user-influenced file path to a real absolute path and confirm
  it stays inside an allowed root before reading or writing it.
- Don't return internal error detail (stack trace, query text, file path)
  to an external caller.
