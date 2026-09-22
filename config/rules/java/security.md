---
id: java/security
title: Java security
applies_to: [Java]
priority: 30
summary: PreparedStatement always, validate deserialization sources, no secrets in code.
---

- Use `PreparedStatement` with bound parameters for every query; never
  concatenate user input into SQL text.
- Avoid deserializing untrusted data with Java's native serialization; use
  a safe, schema-validated format (JSON with a strict mapper) instead.
- Never log a full request/response object that might carry a password,
  token, or session identifier.
- Validate file upload content type and size server-side, not only via the
  client's `accept` attribute.
- Keep secrets in the platform's secret manager or environment, never in a
  properties file committed to the repository.
