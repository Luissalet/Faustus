---
id: php/security
title: PHP security
applies_to: [PHP]
priority: 30
summary: Prepared statements only, escape output per context, validate file uploads server-side.
---

- Use PDO/the query builder's prepared statements with bound parameters;
  never interpolate a variable directly into a SQL string.
- Escape output for its actual context (`htmlspecialchars` for HTML, the
  framework's own URL/attribute escaping) rather than one generic filter
  for everything.
- Validate an uploaded file's real content type and size server-side; never
  trust the client-supplied filename or MIME type alone.
- Use the framework's CSRF token on every state-changing form; don't rely
  on the `Referer` header for that protection.
- Keep secrets in environment variables read via the framework's config
  layer, never committed in a checked-in `.env` or config file.
