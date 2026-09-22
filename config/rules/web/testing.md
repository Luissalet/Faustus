---
id: web/testing
title: Web testing
applies_to: [HTML, CSS, SCSS, JavaScript, TypeScript]
priority: 36
summary: Test across the target browsers/breakpoints that actually matter; wait for real state.
---

- Test at the breakpoints/viewport sizes the product actually targets, not
  just the one the development machine happens to use.
- Wait for a real readiness condition (an element visible, a network call
  settled) instead of a fixed sleep in a browser test.
- Check keyboard-only navigation and screen-reader labelling for anything
  interactive, not just mouse interaction.
- Verify a form's client-side validation still rejects the same input when
  submitted directly (server-side), since client checks are UX, not
  security.
