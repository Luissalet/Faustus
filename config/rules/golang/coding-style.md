---
id: golang/coding-style
title: Go coding style
applies_to: [Go]
priority: 32
summary: gofmt output as-is, short receiver names, package names without stutter.
---

- Run the project through its formatter (`gofmt`/`goimports`) — never
  hand-format Go, and never leave a diff that a formatter would change.
- Use short, conventional receiver names (`s *Server`, not
  `theServerInstance *Server`).
- Avoid stutter in exported names (`user.User`, not `user.UserUser`); the
  package name is part of the caller's read.
- Keep `go vet`/`staticcheck` (if configured) clean; don't add a
  suppression comment to silence a real finding.
- Group related declarations; keep exported API at the top of the file,
  helpers below.
