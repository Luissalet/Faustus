---
name: react-patterns
description: Keep render pure, side effects out of render, state as close to where it's used as possible, and choose composition patterns (slots, compound components) over prop-drilling or inheritance. Use when writing or reviewing React components, hooks, or state management.
version: 1.0.0
category: engineering
tags: [react, frontend, patterns]
status: published
source: imported
---

## When to Use

Writing or reviewing a React component, hook, or piece of client-side state
management. Layered on top of `coding-standards`.

## Procedure

1. **Keep render a pure function of props and state.** No side effects, no
   mutation of anything outside the component during render — a component
   that fetches data directly in its render body (rather than in an effect
   or a data-fetching hook) will re-fire on every re-render for the wrong
   reason.
2. **Put side effects in `useEffect` (or a framework's data-fetching hook)**,
   never inline in the render body, and always with a correct dependency
   array — an effect with a stale or missing dependency is the single most
   common source of "why did this not update" bugs.
3. **Locate state as close to where it's used as possible.** Local
   component state for anything only that component (and its direct
   children) cares about; lift to a shared parent or context only once two
   siblings actually need to stay in sync; reach for a global store only
   for state that's genuinely app-wide (auth, theme, a shopping cart).
4. **Prefer composition over inheritance for sharing behaviour** — a custom
   hook for shared logic, `children`/slot props for shared layout, a
   compound-component pattern (a parent providing context, children
   consuming it) for a family of related pieces that share state without
   prop-drilling it through every level.
5. **Design forms deliberately**: a controlled input for anything the UI
   needs to react to as the user types, an uncontrolled/ref-based approach
   for a simple form that only needs its value on submit, and validation
   error state that's visible without a re-render race.
6. **Wrap risky subtrees in error boundaries** and use `Suspense` for
   loading states at the boundary that actually owns the async dependency,
   not scattered ad-hoc loading flags through every consumer.
7. **Reach for `memo`/`useMemo`/`useCallback` only after measuring**, not by
   default — memoizing something cheap to recompute adds complexity and a
   dependency-array bug risk for no real gain; it earns its place on an
   expensive computation or a component re-rendering visibly too often in
   a real profile.
8. **Build lists with a stable, meaningful `key`** (an ID, never the array
   index for anything that can reorder) — an index key silently corrupts
   component state across a filtered/reordered list.

## Pitfalls

- A `useEffect` with a missing dependency (usually caught by the linter,
  but sometimes deliberately silenced) that reads stale state or props.
- Reaching for a global store for state that's actually only used by one
  component tree, adding indirection with no real payoff.
- Deriving state and storing it in `useState` instead of computing it
  directly from props/state during render — the derived copy drifts the
  moment its source updates and the effect that was supposed to sync it
  fires a render late.
- Prop-drilling five levels deep instead of using `children`/context/a
  compound component once the same data threads through every layer.
- Memoizing everything defensively, which adds bugs (stale closures in a
  `useCallback`) without a measured performance problem to justify it.

## Verification

- No effect fires on every render due to a missing or overly-broad
  dependency array; check by adding a log/counter locally if it's not
  obvious.
- State lives at the lowest common owner of everything that reads it — not
  higher "just in case", not lower and then drilled back up.
- List rendering uses a stable key derived from real identity, not array
  index, for anything the list can reorder or filter.
- A memoization was added only after observing an actual unnecessary
  re-render, not preemptively.
