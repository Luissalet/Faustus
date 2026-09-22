---
id: react/coding-style
title: React coding style
applies_to: [TypeScript/React, JavaScript/React]
priority: 32
summary: Render stays pure; effects handle side effects; hooks called unconditionally.
---

- Keep render a pure function of props and state — no side effect, no
  mutation of anything outside the component during render.
- Call hooks unconditionally, at the top level, in the same order every
  render — never inside a condition, loop, or nested function.
- Put side effects (fetch, subscription, timer) in `useEffect` with a
  correct, complete dependency array, never inline in the render body.
- Name a custom hook starting with `use` and keep it focused on one
  concern.
- Use a stable, meaningful `key` (an ID) for list items, never the array
  index for a list that can reorder or filter.
- Keep a component's JSX and its data-fetching/logic separated enough that
  the logic could be unit-tested without rendering anything.
