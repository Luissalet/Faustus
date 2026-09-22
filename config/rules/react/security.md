---
id: react/security
title: React security
applies_to: [TypeScript/React, JavaScript/React]
priority: 30
summary: Avoid dangerouslySetInnerHTML with unsanitised input; validate before render.
---

- Avoid `dangerouslySetInnerHTML`; when it's genuinely required, sanitise
  the input first with a dedicated library, never with a hand-written
  regex strip.
- Don't put a secret or an API key readable by the client bundle — anything
  shipped to the browser is public.
- Validate/escape a value before putting it into a URL, an `href`, or a
  redirect target built from user input (open-redirect risk).
- Treat any prop or query param rendered as text as untrusted; React
  escapes it by default in JSX — don't work around that escaping.
