---
id: web/coding-style
title: Web (HTML/CSS/JS) coding style
applies_to: [HTML, CSS, SCSS, JavaScript, TypeScript]
priority: 32
summary: Semantic HTML first, scoped styles, progressive enhancement over a JS-only path.
---

- Use the semantic element for the job (`button`, `nav`, `main`, `label`)
  before reaching for a generic `div` with a click handler and ARIA bolted
  on.
- Scope styles (CSS modules, a component-scoped stylesheet, or a consistent
  naming convention) rather than global selectors that can leak across
  unrelated components.
- Keep a page usable with JavaScript disabled or slow to load where
  practical — a core form submit or link navigation shouldn't require a
  script to function at all.
- Avoid inline styles for anything that isn't truly one-off and dynamic;
  keep visual rules in the stylesheet layer.
- Keep bundle size in mind before adding a new dependency for a small UI
  need a few lines of CSS/JS could cover.
