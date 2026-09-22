---
id: web/security
title: Web security
applies_to: [HTML, CSS, SCSS, JavaScript, TypeScript]
priority: 30
summary: CSP, no inline event handlers with unescaped data, safe target=_blank links.
---

- Set a Content-Security-Policy that restricts script sources; avoid
  inline `<script>`/`onclick="..."` built from dynamic data.
- Add `rel="noopener noreferrer"` to a `target="_blank"` link that points
  at content you don't control, to stop the opened page reaching back into
  the opener.
- Escape any user-controlled value placed into an HTML attribute or text
  node — never build a DOM string by concatenation with unescaped input.
- Set cookies used for auth as `HttpOnly` and `Secure`; never rely on a
  client-readable cookie for something that must resist XSS.
- Validate a redirect target against an allow-list rather than following
  an arbitrary user-supplied URL.
