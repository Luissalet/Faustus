---
id: swift/security
title: Swift security
applies_to: [Swift]
priority: 30
summary: Keychain for secrets, ATS enabled, validate deep-link/URL scheme input.
---

- Store a credential/token in the Keychain, never in `UserDefaults` or a
  plain file.
- Keep App Transport Security enabled; don't add a blanket exception to
  allow arbitrary insecure loads.
- Validate and allow-list input arriving through a custom URL scheme or
  universal link before acting on it — it's an entry point any other app
  can call.
- Don't log a full response body that might contain a token or personal
  data in a release build.
