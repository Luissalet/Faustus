---
id: react/patterns
title: React patterns
applies_to: [TypeScript/React, JavaScript/React]
priority: 34
summary: State lives at its lowest common owner; composition over prop-drilling; memoize only after measuring.
---

- Locate state at the lowest common owner of everything that reads it —
  not lifted "just in case", not duplicated lower down.
- Prefer composition (`children`, slots, a compound-component pattern) over
  prop-drilling the same value through several levels.
- Reach for `memo`/`useMemo`/`useCallback` only after observing an actual
  unnecessary re-render, not by default.
- Wrap a risky subtree in an error boundary; use `Suspense` at the boundary
  that owns the async dependency, not scattered ad-hoc loading flags.
- Derive a value from props/state during render instead of syncing it into
  a second `useState` with an effect.
