---
id: web/patterns
title: Web patterns
applies_to: [HTML, CSS, SCSS, JavaScript, TypeScript]
priority: 34
summary: Debounce/throttle expensive handlers, lazy-load below the fold, design mobile-first.
---

- Debounce or throttle a handler that fires on scroll/resize/input and does
  real work — running expensive logic on every event is a common jank
  source.
- Lazy-load images and non-critical scripts below the fold rather than
  loading everything eagerly on first paint.
- Design layout mobile-first (base styles for small screens, `min-width`
  media queries to add complexity) rather than the reverse.
- Use `fetch`/an abort controller to cancel a stale in-flight request when
  a newer one supersedes it (e.g. search-as-you-type).
- Keep a form's error state visible next to the field it belongs to, not
  only in a summary the user has to scroll to find.
