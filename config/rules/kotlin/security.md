---
id: kotlin/security
title: Kotlin security
applies_to: [Kotlin]
priority: 30
summary: Parameterised queries, EncryptedSharedPreferences for secrets, validate deep links.
---

- Use parameter binding (Room/Exposed's own placeholders) for a query;
  never string-concatenate user input into SQL.
- Store a credential on Android in `EncryptedSharedPreferences`/the
  Keystore, never plain `SharedPreferences`.
- Validate and allow-list input arriving through a deep link/intent before
  acting on it — it's reachable from any other app on the device.
- Avoid logging a full request/response body that might carry a token or
  personal data in a release build.
