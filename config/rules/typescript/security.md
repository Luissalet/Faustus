---
id: typescript/security
title: TypeScript security
applies_to: [TypeScript]
priority: 30
summary: Never inject into a template literal that becomes HTML, SQL, or a shell command.
---

- Never build HTML by string concatenation/template literal with unescaped
  user input; use the framework's own escaping or a sanitiser for anything
  rendered as raw HTML.
- Never build a SQL query or shell command from a template literal with
  untrusted input; use parameterised queries and argument arrays.
- Validate a request body against a schema (a runtime validator, not just
  a compile-time type) at the API boundary — a TypeScript type is erased at
  runtime and enforces nothing there.
- Never trust a JWT's claims without verifying its signature against the
  expected issuer/audience first.
- Don't store a secret or token in `localStorage`/a client-readable cookie
  without weighing the XSS exposure that gives it.
