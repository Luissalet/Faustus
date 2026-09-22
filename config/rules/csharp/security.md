---
id: csharp/security
title: C# security
applies_to: [C#]
priority: 30
summary: Parameterised commands, validate model binding, secrets via configuration providers.
---

- Use parameterised `SqlCommand`/an ORM's parameter binding; never build a
  query with string interpolation of user input.
- Validate model-bound request data with data annotations or a validation
  library server-side, even when the client already validates.
- Read secrets through the configuration/secret-manager provider, never
  hardcoded or committed in `appsettings.json`.
- Set an explicit `HttpClient` timeout for outbound calls; don't rely on
  the default.
- Encode output that reaches a view/response; don't disable Razor's
  default HTML encoding without a specific, reviewed reason.
